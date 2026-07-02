"""Story logic pipeline: writer/brief/audit prompt systems, logic-hole audit,
script doctor, crowding/no-name/overlength detectors, logic brief, canon
extraction and rollback."""
import json
import math as _math
import re
from collections import Counter

from sw.jsonutils import _strip_markdown_fence, loads_lenient, strip_json
from sw.llm import claude_ask, claude_ask_fast
from sw.logging_utils import _log_event
# routes->helpers exception: trajectory/finale helpers live in landmarks
# (no cycle: landmarks does not import story_logic).
from sw.routes.landmarks import build_trajectory_block, is_finale_episode
from sw.storage import (DEVICE_TAXONOMY, NARRATIVE_ARCHETYPES,
                        NARRATIVE_EMOTIONS, WORLD_RULES, _next_id,
                        _resolve_char_by_script_name, load_canon, load_episode,
                        load_series, save_canon)
from sw.story_prompts import _format_mode_block

# ── Production pipeline ───────────────────────────────────────────────────────

MILESTONE_EPS = [1, 10, 20, 30, 40, 50, 60, 70]

_WRITER_SYSTEM = (
    "You are a professional screenwriter specializing in short-form drama series for TikTok and Reels. "
    "LANGUAGE RULES — NON-NEGOTIABLE: "
    "Write ALL synopses, descriptions, and story text in RUSSIAN. "
    "Character names must be English or Western European (e.g. Claire, Marcus, Elena, James) — "
    "never Russian, Chinese, Korean, Japanese or other non-Western names — "
    "but all surrounding text must be in Russian. "
    "LOCATION NAMES — ALSO ENGLISH ONLY, NON-NEGOTIABLE: "
    "Every location.name field must be in ENGLISH (e.g. 'Hotel Room', 'Base HQ Office', 'Medical Bay', 'HQ Corridor', 'Penthouse Bedroom', 'Boardroom'). "
    "FORBIDDEN: Russian location names like 'Военная база', 'Гостиничный номер', 'Штаб', 'Коридор', 'Медицинский пункт' — these are BANNED. "
    "Only the location.description may be in Russian. The name itself MUST be English. "
    "Same rule for character names: name field is English, description is Russian. "
    "SHORT DRAMA FORMAT RULES — MANDATORY: "
    "Every synopsis must open with immediate stakes — something is already at risk, in motion, or being revealed. No warm-up, no 'в этом эпизоде герой узнаёт...'. "
    "Hook varieties: шокирующая находка, неожиданный приход, холодное убийственное разоблачение, ложь пойманная на полуслове, отчаянный шаг уже в действии — варьируй, не повторяй одно и то же. "
    "Every episode needs three beats: крючок с немедленными ставками, разворот в середине (что-то что мы считали правдой оказывается ложью или власть резко меняется), и клиффхэнгер в конце. "
    "FORBIDDEN: медленное развитие, мирные открывающие сцены, любое начало которое постепенно нагнетает. "
    "Pacing rule: если в синопсисе из 3 предложений нет разворота или твиста — перепиши. "
    "DIALOGUE-FIRST RULE — HARD BAN ON PAPERWORK & SCREENS: "
    "СИНОПСИС ОПИСЫВАЕТ КАДР, А КАДР — ЭТО ДВА ЧЕЛОВЕКА В КОНФЛИКТЕ. "
    "Все откровения, развороты и клиффхэнгеры должны передаваться через УСТНЫЕ КОНФРОНТАЦИИ — обвинение, угроза, признание, насмешка, ультиматум — лицом к лицу. "
    "ЗАПРЕЩЕНО использовать в качестве носителя сюжета: "
    "письма, записки, документы, контракты, файлы, досье, папки с фотографиями, "
    "SMS, сообщения в мессенджерах, чаты, e-mail, "
    "экраны телефонов/ноутбуков/компьютеров, любые UI-экраны, "
    "записи с камер видеонаблюдения, диктофонные записи которые слушают в кадре, "
    "дневники, voiceover, газетные заголовки, новости по ТВ/радио, "
    "немые флэшбеки, монтажи без диалога. "
    "НЕ ПИШИ фразы вида: «обнаруживает на ноутбуке папку с фото», «получает SMS с угрозой», «на экране телефона видна запись», «находит письмо», «открывает досье», «слышит запись». "
    "ВМЕСТО ЭТОГО пиши: персонаж А сталкивается с персонажем Б и говорит/обвиняет/признаётся вслух. Например: вместо «находит фото James с врагом» — «James сам признаётся ей в лицо, что встречался с тем человеком — но не за тем, что она думает». "
    "УЗКОЕ ИСКЛЮЧЕНИЕ — максимум ОДИН раз НА ВЕСЬ СЕРИАЛ (не на эпизод — на 70 серий целиком): "
    "коротко показать физический предмет (кольцо, тест на беременность, ключ, одно фото), но в том же предложении персонаж проговаривает смысл вслух другому персонажу. "
    "Если этот лимит уже израсходован раньше — НИКАКИХ предметов-носителей сюжета. Только устные конфронтации. "
    "Если в синопсисе появилось слово «папка», «файл», «документ», «экран», «запись», «SMS», «сообщение», «ноутбук с …», «телефон с …», «фото на …», «конверт», «записка», «диктофон», «улики», «доказательства» — ПЕРЕПИШИ через диалог. "
    "HARD BAN — LEGAL/COURTROOM PLOT ENGINES (ЖЁСТКИЙ ЗАПРЕТ НА ЮРИДИЧЕСКИЕ ДВИЖКИ): "
    "Сериал НЕ должен сводиться к суду, юридическому процессу, сбору улик, заседаниям, прокурору, адвокату, судье, иску, обвинительному акту, daw enforcement, депозиции, слушанию, экспертизе, юридическому разбирательству. "
    "ЗАПРЕЩЕНЫ как двигатели сюжета: lawsuit, court case, trial, hearing, deposition, motion, prosecutor, attorney, paralegal, judge, jury, courtroom, indictment, plea, settlement, eviction proceedings, custody hearing, restraining order filing, forensic accountant, evidence-gathering arc, «she sues them», «он подаёт иск», «суд решит», «доказательства против него», «давать показания», «допрос в суде», police investigation arc, FBI raid as climax, prosecutor briefing scene. "
    "Если синопсис серии или арки естественно идёт к суду — ПЕРЕПИСЫВАЙ. "
    "Замена: прямая личная конфронтация / преследование / шантаж / побег / физическое столкновение / разоблачение лицом к лицу / предательство со стороны близкого / угроза похищения / угроза ребёнку / выбор «уйти или остаться» / семейная тайна выходит наружу. Люди — людям, не бумагам и не судьям. "
    "Закон может СУЩЕСТВОВАТЬ в мире сериала как фон или угроза (полицейский звонит в дверь, юрист звонит по телефону — на 5 секунд), но НЕ должен становиться двигателем серии или арки. Максимум на весь сериал: 1 короткая сцена с legal-фоном (≤30 секунд экрана, нерешающая) — если без неё никак. Никаких длинных сцен в зале суда, никаких подготовок к процессу, никаких сборов доказательств как самостоятельной линии. "
    "Если в синопсисе появилось «иск», «суд», «судится», «прокурор», «адвокат», «свидетель», «улики», «доказательства», «расследование», «приговор», «слушание», «истец», «ответчик» как двигатель сюжета — ПЕРЕПИШИ через личную конфронтацию или физическое действие. "
    "ПРОВЕРКА ЛОГИКИ — ОБЯЗАТЕЛЬНО: перед финализацией любого синопсиса проверь временную линию. "
    "ПРОВЕРКА ЛОГИКИ — ОБЯЗАТЕЛЬНО: перед финализацией любого синопсиса проверь временную линию. "
    "Если прошли годы с момента секса — персонаж НЕ беременная сейчас от того эпизода. У неё есть РЕБЁНОК N лет. "
    "Беременность = недавнее событие (недели/месяцы назад). Тайный ребёнок = давнее событие + уже родившийся ребёнок. "
    "Никогда не смешивай эти два тропа. Если математика не сходится — перепиши. "
    "ЯВНОЕ ЗАЧАТИЕ — ОБЯЗАТЕЛЬНО для текущей беременности: "
    "Если в синопсисе есть текущая беременность, в нём ДОЛЖНО быть явно названо НЕДАВНЕЕ событие (последние недели, максимум ~3 месяца), когда произошло зачатие. "
    "Читатель не должен додумывать. Не пиши «три года назад он разрушил её семью. Теперь она беременна от него» — это двусмысленно. "
    "Пиши «три года спустя они оказались в одной постели на благотворительном вечере — а через шесть недель она узнаёт, что беременна от человека, которого ненавидит». "
    "Запрещённые паттерны: «N лет назад [событие] … она беременна от него» без явной недавней ночи; «спустя годы она узнаёт что беременна»; смешение давней мести и текущей беременности без явного зачатия. "
    "Если ты не можешь уместить явный момент зачатия — переключайся на тайного ребёнка (тогда беременности нет, есть ребёнок N лет). "
    "Respond ONLY with valid JSON — no markdown fences, no commentary."
)
# ════════════════════════════════════════════════════════════════════════════
# LOGIC PIPELINE — pre-write brief, post-write audit, canon auto-extract.
# Three lightweight Claude calls wrapped around every script generation.
# Fully automated: violations trigger silent regeneration, never user prompts.
# ════════════════════════════════════════════════════════════════════════════

_BRIEF_SYSTEM = (
    "You are a continuity producer for a short-drama TV series. "
    "Given the series canon, the upcoming episode synopsis and recent episodes, "
    "produce a CONCISE constraints brief in RUSSIAN that the script writer MUST respect. "
    "Be specific, numerical, and short. Do NOT write narrative — write rules. "
    "Output PLAIN TEXT (no JSON, no markdown fences)."
)

_AUDIT_SYSTEM = (
    "You are a strict continuity editor for short-drama scripts. "
    "Compare the SCRIPT against the CANON and the LOGIC BRIEF. "
    "Find every contradiction, timeline impossibility, knowledge leak (character knows "
    "something they couldn't know), biology/physics violation, unresolved required setup, "
    "or IMPOSSIBLE NARRATOR PERSPECTIVE. "
    "IMPOSSIBLE NARRATOR PERSPECTIVE: flag as type='knowledge', severity='critical' when a character "
    "in a letter/diary/note refers to their OWN DEATH in past tense while the text was written BEFORE "
    "they died (e.g. 'Three weeks before I died...' in a letter written by the deceased — impossible, "
    "they could not know they would die). Correct form: 'before what I fear will be my end', "
    "'should something happen to me', or simply present-tense 'three weeks ago'. "
    "Also flag when any character demonstrates knowledge of future events they could not have known. "
    "Be ruthless but precise — only flag REAL contradictions backed by canon, not stylistic notes. "
    "ALSO flag SCENE TELEPORTATION as type='scene_teleport', severity='critical': "
    "if the PREVIOUS EPISODE script ended mid-scene (a character had just arrived / a question was hanging / "
    "two characters were standing face to face mid-confrontation / a reaction shot was the cliffhanger), "
    "then THIS episode MUST open in the same location with the same characters present, continuing that scene. "
    "If this episode instead opens in a different room, with a different speaker configuration, with the same "
    "antagonist now 'summoning' someone they were already in front of, or with a 'later/next morning' timestamp "
    "that abandons the unresolved confrontation — flag it as scene_teleport critical. "
    "Exception: scene change is fine only if the previous episode genuinely closed its scene (private decision, "
    "character walked out, explicit time-jump cliffhanger). When in doubt, flag it. "
    "ALSO flag any DIALOGUE-FIRST RULE violations as type='paperwork', severity='critical': "
    "any reveal carried by a letter, note, document, file, contract, dossier, envelope, evidence binder, "
    "text message, SMS, chat bubble, email, on-screen UI, computer/phone screen, photograph handed over, "
    "USB drive / flash card / hidden recording being played, surveillance/CCTV being watched, "
    "diary, voiceover, news headline, radio report, or silent flashback montage. "
    "Reveals MUST come through spoken dialogue (accusations, taunts, confessions). "
    "A short physical object (ring, test, key, photo) may appear silently for 1–2s only ONCE in the "
    "entire series IF a character immediately verbalizes its meaning aloud — not once per episode. "
    "A single note ≤6 words is allowed only if the punch hinges on those exact words and there "
    "is no spoken alternative. Otherwise → flag as critical paperwork violation. "
    "ALSO flag any LEGAL/COURTROOM PLOT ENGINE as type='legal_engine', severity='critical': "
    "the episode is driven by a lawsuit, court case, trial, deposition, hearing, plea, settlement, "
    "indictment, evidence-gathering arc, lawyer strategy session, prosecutor briefing, courtroom scene "
    "(cross-examination / verdict / judge / jury), 'we need proof to win in court', 'see you in court', "
    "'I'm filing tomorrow', 'the case goes to trial', police-investigation procedural arc as the "
    "primary engine, raid as climax, or scene set in COURTROOM / LAW FIRM / JUDGE'S CHAMBERS / "
    "DEPOSITION ROOM / PROSECUTOR'S OFFICE / EVIDENCE LOCKER / DA'S OFFICE as the scene that delivers "
    "the episode's main turn. Law may exist as one-line atmosphere (a detective calls, a lawyer is "
    "mentioned in passing) but never as the engine. Fix: rewrite the beat as face-to-face personal "
    "confrontation — accusation, blackmail, ultimatum, chase, betrayal, physical clash, exposure in "
    "front of a third party. People against people, not people against the legal system. "
    "Severity: 'critical' = breaks the story logic OR violates dialogue-first rule; 'minor' = "
    "inconsistency but watchable. "
    "LANGUAGE RULES FOR THE OUTPUT — STRICT: "
    "• 'where' (description of which beat/line is broken) → RUSSIAN. "
    "• 'explanation' (what's wrong and why) → RUSSIAN. "
    "• 'fix' → keep the script's languages: dialogue replacement lines in ENGLISH, "
    "action-line replacements in RUSSIAN. Inside one 'fix' value you may mix both if it spans both. "
    "Do NOT translate dialogue replacements into Russian — those must stay English so the writer can paste them in. "
    "Respond ONLY with valid JSON: "
    '{"passes": bool, "violations": [{"type":"timeline|fact|knowledge|biology|setup|paperwork|legal_engine|scene_teleport", '
    '"severity":"critical|minor", "where":"описание места по-русски", "explanation":"что не так — по-русски", "fix":"replacement (English dialogue / Russian action)"}]} '
    "passes=true ONLY if zero critical violations."
)

_LOGIC_HOLE_AUDIT_SYSTEM = (
    "You are a sharp story-logic editor for short-drama scripts. "
    "Your job is to find STORY LOGIC HOLES that would make a viewer think 'wait, that doesn't make sense' — "
    "the kind of issues a smart audience catches in 5 minutes of reading. "
    "You are NOT looking for stylistic notes, pacing, or canon contradictions (a separate auditor handles those). "
    "Look ONLY for these specific kinds of holes:\n"
    "\n"
    "1. AUTHORITY/STATUS MISMATCH (type='status'): a character's legal/corporate status is unclear or "
    "self-contradictory. Examples: a character claims ownership of a company while another says their "
    "contract expires soon (owner vs. employee). The script must be internally consistent about WHO has what power "
    "(owner / heir / CEO / contracted creative director / employee / board member). "
    "If two beats imply different statuses for the same character — flag it.\n"
    "\n"
    "2. UNJUSTIFIED HIDDEN POSITION (type='hidden_position'): a character holds secret power "
    "(secretly the heir, secretly the boss, secretly armed) but the script never gives a one-line reason "
    "why they choose to remain in their visible 'lower' position. The audience needs to hear / read "
    "a single motive line: 'I stayed on the floor to see what kind of man was running my company.' "
    "Without it, the scenario reads like an oversight, not a strategy.\n"
    "\n"
    "3. MISSING ENABLING CONDITION (type='enabling_condition'): the antagonist (or anyone) takes a "
    "major action — public press conference, firing someone, signing a contract, accessing a system — "
    "after the power dynamic should have stopped them, and the script never explains WHY they still could. "
    "Required: a one-line setup explaining the loophole — 'contract not yet signed', 'board hadn't issued "
    "transition statement yet', 'press preview was already scheduled', 'PR team still reports to him'.\n"
    "\n"
    "4. LEGAL TERM MISMATCH (type='legal_term'): a character uses a strong legal/criminal term "
    "(criminal history, fraud, theft of identity, defamation) but the underlying facts they list "
    "do NOT support that term (e.g. 'criminal history' followed by 'cocktail waitress, hostess, escort' — "
    "those are jobs, not crimes). Either the term must change to fit the facts (compromising past, "
    "the life she tried to bury) or the facts must support the term (fraud, aliases, payments).\n"
    "\n"
    "5. UNMOTIVATED DELAY (type='unmotivated_delay'): a group (board, family, ally) sat on critical "
    "information for days/weeks without a stated reason. The script needs ONE concrete reason — "
    "consolidating accounts, gathering evidence, waiting for legal transition, protecting the protagonist. "
    "Vague 'we needed time' without specifics = flag.\n"
    "\n"
    "6. AMBIGUOUS CLIFFHANGER (type='ambiguous_cliffhanger'): the final line is so vague the viewer "
    "doesn't know what just happened. 'Let's go' / 'Watch this' / 'You'll see' without context. "
    "Cliffhanger should imply a clear next move (release the file, publish, expose, leave) even if "
    "the resolution is held back. Suggest a sharper alternative. "
    "EXCEPTION: if the context marks this episode as THE SERIES FINALE, a conclusive resolution with "
    "NO cliffhanger is CORRECT — do NOT flag ambiguous_cliffhanger or a missing cliffhanger; finale "
    "closure problems are handled by finale_drift instead.\n"
    "\n"
    "7. PLOT REPETITION (type='plot_repetition'): the script uses a narrative delivery mechanism "
    "(written_message, overheard_dialogue, phone_call_stranger, dream_flashback, confession_direct, "
    "discovery_object, confrontation_domestic, confrontation_public, betrayal_reveal, rescue_escape, "
    "legal_threat, ally_arrives, surveillance_caught, blackmail, accident_staged) that was already "
    "used 3+ times in this series according to the PLOT DEVICES context provided. "
    "Severity: 'minor'. Fix: suggest a concrete alternative mechanism from the available list.\n"
    "\n"
    "8. PROTAGONIST STAGNATION (type='protagonist_stagnation'): the protagonist ends this episode "
    "in the same or worse position AND has not made any concrete progress toward their goal. "
    "Flag ONLY when the NARRATIVE MOMENTUM context shows 4+ consecutive 'antagonist_wins' episodes. "
    "Severity: 'minor'. Fix: suggest one concrete thing the protagonist achieves or changes by episode end.\n"
    "\n"
    "9. EMOTIONAL MONOTONY (type='emotional_monotony'): the episode's closing emotional beat is "
    "identical to the previous 3+ episodes (same closing_emotion pattern from NARRATIVE MOMENTUM). "
    "Flag ONLY when the pattern is clear from the context provided. "
    "Severity: 'minor'. Fix: suggest a different emotional resolution that still fits the story logic.\n"
    "\n"
    "10. SCENE OVERCROWDING (type='scene_overcrowding'): a scene has more speaking characters than the "
    "series limit specified in the LOGIC CONSTRAINTS brief under 'ЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ'. "
    "Count ONLY characters who speak at least one line or perform a named action — not background crowd/extras. "
    "Flag ONLY when the LOGIC CONSTRAINTS brief explicitly states a limit AND the script violates it. "
    "Severity: 'critical'. Fix: specify which character(s) to remove from the scene and how to restructure it "
    "(e.g. split into two consecutive scenes, or cut secondary characters to single-line cameos).\n"
    "\n"
    "11. FINALE DRIFT (type='finale_drift'): the script CONTRADICTS or makes impossible the user-pinned "
    "STORY TRAJECTORY (finale / checkpoints) that appears at the top of the LOGIC CONSTRAINTS brief. "
    "Trigger when the script: (a) kills, exposes, jails, or otherwise neutralizes a character the finale "
    "or an upcoming checkpoint needs in a specific state; (b) resolves a conflict the finale needs "
    "unresolved; (c) introduces a competing climax that steals the finale's moment; (d) when this IS "
    "the finale episode (distance = 0, or the context carries a SERIES FINALE note) — fails to execute "
    "the finale's specified events with the specified characters, OR ends on a cliffhanger / new threat / "
    "new mystery / a hook into a non-existent next episode, OR leaves a major thread deferred instead of "
    "resolving it on screen ('promised for tomorrow', 'to be addressed', 'reckoning later', a 'Setup for "
    "next episode' note) — a finale must CLOSE every main thread; (e) introduces a NEW major villain / "
    "culprit / love interest that the user finale or checkpoints never reference. "
    "Severity: 'critical'. Fix: identify which finale/checkpoint constraint is violated and propose "
    "a rewrite (specific lines / actions to change) that keeps the trajectory intact. "
    "If no STORY TRAJECTORY block AND no SERIES FINALE note is present in the context — DO NOT flag this type.\n"
    "\n"
    "Severity rules: 'critical' = breaks viewer's suspension of disbelief (can't follow the story) "
    "OR contradicts the user-pinned trajectory; "
    "'minor' = noticeable on rewatch but doesn't break first-viewing.\n"
    "\n"
    "LANGUAGE RULES FOR THE OUTPUT — STRICT:\n"
    "• 'where' → RUSSIAN. Опиши место по-русски (например: «реплика Marcus в третьей сцене», «финальная строка эпизода»). "
    "Quoted English line snippets are allowed inside the Russian description if needed for precision.\n"
    "• 'explanation' → RUSSIAN. Объясни по-русски, что именно ломает логику и что зритель заметит.\n"
    "• 'fix' → preserve the script's languages: dialogue replacement lines in ENGLISH, action-line additions in RUSSIAN. "
    "Do NOT translate dialogue into Russian — the writer must be able to paste it straight into the script.\n"
    "\n"
    "Be PRECISE in 'where', be SPECIFIC in 'fix' — write the exact replacement line, not a vague suggestion.\n"
    "\n"
    "Output ONLY valid JSON: "
    '{"passes": bool, "violations": [{"type":"status|hidden_position|enabling_condition|legal_term|unmotivated_delay|ambiguous_cliffhanger|plot_repetition|protagonist_stagnation|emotional_monotony|scene_overcrowding|finale_drift", '
    '"severity":"critical|minor", "where":"описание места по-русски", "explanation":"что не так и почему зритель заметит — по-русски", '
    '"fix":"replacement (English dialogue / Russian action)"}]} '
    "passes=true ONLY if zero critical violations."
)


def audit_logic_holes(sid, num, script):
    """Second-pass auditor: catches story-logic holes (status mismatches, missing enabling
    conditions, unmotivated hidden positions, legal-term mismatches, vague cliffhangers).
    Complementary to audit_script which handles canon/continuity. Never raises.

    Includes the previous episode's script as context so the auditor doesn't flag
    things as 'unmotivated' when the motivation was actually established earlier.
    """
    s = load_series(sid) or {}
    ep = load_episode(sid, num) or {}

    # Pull previous episode's script + synopsis to give auditor cross-episode context.
    prev_block = ''
    if num > 1:
        prev_ep = load_episode(sid, num - 1) or {}
        prev_script = (prev_ep.get('script') or '').strip()
        prev_syn    = (prev_ep.get('synopsis') or '').strip()
        if prev_script or prev_syn:
            # Trim previous script so we don't blow the context window — keep last ~2500 chars
            # (covers the typical short-drama episode end-state) plus the synopsis.
            tail = prev_script[-2500:] if len(prev_script) > 2500 else prev_script
            prev_block = (
                f'=== PREVIOUS EPISODE ({num-1}) — context only, do NOT audit ===\n'
                f'Synopsis: {prev_syn}\n\n'
                f'Script (tail):\n{tail}\n'
                f'=== END PREVIOUS EPISODE ===\n\n'
                'IMPORTANT: if a motive, setup, or enabling condition for episode '
                f'{num} was already established in episode {num-1} above, do NOT flag '
                'it as missing — treat it as already justified.\n\n'
            )

    # Build plot-device history for repetition auditing
    try:
        audit_devices_block = _build_plot_device_history(sid, num)
    except Exception:
        audit_devices_block = ''

    # Finale awareness — this auditor does NOT receive the trajectory brief, so
    # surface the finale flag explicitly. Without it, the conclusive finale
    # ending gets mis-flagged as ambiguous_cliffhanger and rewritten back into a
    # hook, and a finale that fails to resolve goes uncaught.
    finale_block = ''
    if is_finale_episode(s, num):
        fin_desc = ((s.get('finale') or {}).get('description') or '').strip()
        finale_block = (
            '\n=== ⚠ THIS EPISODE IS THE SERIES FINALE (last episode) ===\n'
            'A conclusive ending with NO cliffhanger is CORRECT here — do NOT flag '
            'ambiguous_cliffhanger or a missing cliffhanger. INSTEAD flag finale_drift '
            '(critical) if the script: ends on a cliffhanger / new threat / new mystery / '
            'a hook into a non-existent next episode; leaves a major thread deferred '
            '("promised for tomorrow", "to be addressed", "Setup for next episode"); or '
            'fails to deliver the pinned finale end-state below.\n'
            f'PINNED FINALE END-STATE:\n{fin_desc}\n'
            '=== END FINALE NOTE ===\n\n'
        )

    context = (
        f'Series: "{s.get("title") or ""}" | Genre: {s.get("genre") or ""}\n'
        f'Series arc: {(s.get("arc") or "")[:600]}\n'
        f'Episode {num} synopsis: {ep.get("synopsis") or ""}\n\n'
        + finale_block
        + (audit_devices_block if audit_devices_block else '')
        + prev_block
        + f'=== SCRIPT TO AUDIT (episode {num}) ===\n{script}\n\n'
        'Find every STORY LOGIC HOLE per the schema. Be ruthless about the 7 categories. '
        'Reminder: only flag issues in the EPISODE-TO-AUDIT script. Use the previous-episode '
        'context purely to avoid false positives on things already established.'
    )
    try:
        raw = claude_ask_fast(context, system=_LOGIC_HOLE_AUDIT_SYSTEM)
        data = loads_lenient(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
        # Last-ditch repair: try just regex-stripping fences + lenient parse
        try:
            data = loads_lenient(_strip_markdown_fence(raw))
            if isinstance(data, dict):
                data.setdefault('violations', [])
                data['passes'] = bool(data.get('passes', not any(
                    v.get('severity') == 'critical' for v in data['violations']
                )))
                return data
        except Exception:
            pass
        _log_event('WARN', 'audit_json_parse_fail', err=str(e)[:200], raw_head=raw[:200] if 'raw' in dir() else '')
        return {'passes': True, 'violations': [], 'audit_error': str(e)}


_SCRIPT_DOCTOR_SYSTEM = (
    "You are a script doctor for short-drama series. You receive an existing script plus "
    "a list of story-logic holes (status mismatch, hidden position not motivated, missing enabling "
    "condition, legal term mismatch, unmotivated delay, vague cliffhanger). "
    "Your job: produce a MINIMALLY-EDITED version of the script that fixes EVERY listed issue. "
    "Rules:\n"
    "- Preserve everything that is not broken — same scene structure, same beat order, "
    "same character names, same EPISODE CAST block, same cliffhanger structure. "
    "- Apply the smallest possible edits to fix each hole: usually 1–2 added or replaced lines per issue. "
    "- Do not rewrite working scenes for style. Do not change the genre or tone. "
    "- Keep the language rules intact: dialogue in English, action lines in Russian, scene headings as-is. "
    "- If the cast block exists, keep it identical. "
    "- If EPISODE NOTES / CHUNK NOTES tail exists, keep it (update only if the cliffhanger line changed). "
    "Output ONLY the corrected script — no commentary, no diff, no JSON, no markdown fences. Just the script text."
)


def doctor_script(sid, num, script, violations):
    """Take existing script + list of logic-hole violations → return surgically-fixed script."""
    s = load_series(sid) or {}
    ep = load_episode(sid, num) or {}
    fixes_block = '\n'.join(
        f'- [{v.get("type","?")}] WHERE: {v.get("where","?")} | PROBLEM: {v.get("explanation","")} | REQUIRED FIX: {v.get("fix","")}'
        for v in violations
    ) or '(no specific issues — return script unchanged)'
    prompt = (
        f'Series: "{s.get("title") or ""}" | Genre: {s.get("genre") or ""}\n'
        f'Episode {num} synopsis: {ep.get("synopsis") or ""}\n\n'
        f'=== SCRIPT TO PATCH ===\n{script}\n\n'
        f'=== LOGIC HOLES TO FIX ===\n{fixes_block}\n\n'
        'Return the corrected script with minimal edits. Output the full script text only.'
    )
    return claude_ask(prompt, system=_SCRIPT_DOCTOR_SYSTEM)


_EXTRACT_SYSTEM = (
    "You are a canon archivist. Read the script and extract structured canon updates. "
    "Be conservative — only record facts EXPLICITLY shown or stated in the script. "
    "Output ONLY valid JSON, no commentary. "
    'Schema: {"world_day_advance": int (days since previous episode, default 1 if unclear), '
    '"new_facts": [{"fact":"short sentence in Russian", "supersedes":"F### or null"}], '
    '"events": ["short event description in Russian", ...], '
    '"character_updates": {"<CharName>": {"learned":["short fact in Russian", ...], '
    '"physical":{"key":"value"}, "location":"loc name or null"}}, '
    '"threads_opened": [{"question":"unresolved question raised in Russian"}], '
    '"threads_closed": ["T### that was resolved this episode", ...]}'
)


from sw.story_prompts import _format_canon_for_prompt
from sw.canon_index import (
    _extract_devices_from_script,
    _extract_narrative_state_from_script,
    _update_devices_index,
    _update_narrative_index,
    _build_plot_device_history,
)
def _build_crowd_constraint_block(s) -> str:
    """Build a hard scene character-count constraint block from the series settings.
    Returns empty string when max_main_chars_per_scene is not configured.
    """
    try:
        n = int(s.get('max_main_chars_per_scene') or 0)
    except (TypeError, ValueError):
        return ''
    if n < 1 or n > 6:
        return ''
    if n == 1:
        rule = (
            'ОДИНОЧНЫЕ СЦЕНЫ: ровно 1 главный персонаж на сцену. '
            'Второй может зайти максимум на 1-2 реплики (вошёл → сказал → ушёл). '
            'Сцены с двумя полноценными участниками — запрещены.'
        )
    elif n == 2:
        rule = (
            '2 ГЛАВНЫХ ПЕРСОНАЖА В СЦЕНЕ — строгий максимум. '
            'Третий персонаж может появиться только чтобы произнести ровно одну реплику-объявление и уйти. '
            'НИКАКИХ сцен где 3+ именованных персонажа одновременно участвуют в диалоге или конфликте.'
        )
    else:
        rule = (
            f'МАКСИМУМ {n} ГЛАВНЫХ ПЕРСОНАЖА в одной сцене. '
            f'В большинстве сцен — 2. До {n} — только для финальной кульминационной конфронтации, '
            f'не чаще одного раза за серию. '
            f'Сцены с {n+1}+ именованными участниками — категорически запрещены.'
        )
    return (
        f'═══ HARD RULE: ЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ ═══\n'
        f'НАСТРОЙКА СЕРИАЛА: не более {n} главных персонажей одновременно в одной сцене.\n'
        f'{rule}\n'
        f'НЕ считается: массовка, гости, прохожие, охрана без реплик — лимит ТОЛЬКО на тех, '
        f'кто говорит или выполняет действие в сцене.\n'
        f'НАРУШЕНИЕ = сцена переписывается. Это жёсткое ограничение продакшна.\n'
        f'Если для сюжета нужно собрать больше {n} персонажей — РАЗБЕЙ на несколько '
        f'последовательных сцен (один уходит → другой заходит) ИЛИ дай большинству молчать '
        f'в кадре (только {n} реально говорящих/действующих, остальные — фон).\n'
        f'═══════════════════════════════════════════════\n\n'
    )


def _count_speaking_characters_per_scene(script: str, cast_names: list[str]) -> list[dict]:
    """Programmatic scan of a generated script to count NAMED-CAST characters with speaking
    lines or named actions in each scene. Returns one dict per scene:
        {scene_idx, location, characters: [names], count}

    Scenes are delimited by INT./EXT./ИНТ./ЭКСТ. headers OR by [BLOCKING] open/close fences.
    A character "speaks" when a line matches `NAME:` at the start (allowing English/Cyrillic).
    A character is "named in action" when their cast name appears as a word in an action-line.
    """
    if not script or not cast_names:
        return []
    cast_set = {n.strip() for n in cast_names if n and n.strip()}
    # Split into scenes — primary delimiter: INT./EXT./ИНТ./ЭКСТ. headers.
    scene_header_re = re.compile(
        r'^\s*(?:INT\.|EXT\.|ИНТ\.|ЭКСТ\.|INT/EXT\.)\s+(.+?)(?:\s+—|\s+–|\s+-|\s+/|\s*$)',
        re.IGNORECASE | re.MULTILINE,
    )
    # Cut-marker break too (for batch chunks)
    cut_marker_re = re.compile(r'═══\s*END\s+EPISODE\s+\d+', re.IGNORECASE)

    # Find all scene start positions
    starts = []
    for m in scene_header_re.finditer(script):
        starts.append((m.start(), m.group(1).strip()))
    for m in cut_marker_re.finditer(script):
        starts.append((m.start(), '<cut>'))
    starts.sort(key=lambda x: x[0])

    if not starts:
        # No scene headers — treat whole script as one scene
        starts = [(0, 'whole script')]

    # Build scene chunks
    scenes = []
    for i, (pos, loc) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(script)
        chunk = script[pos:end]
        scenes.append({'idx': i + 1, 'location': loc, 'text': chunk})

    # Dialogue line pattern: "NAME: ..." (case-insensitive matching against cast)
    # Capture token before colon at line start (allow leading whitespace).
    dialogue_line_re = re.compile(r'^\s*([A-ZА-ЯЁ][A-ZA-Zа-яёА-ЯЁ\.\-\' ]{1,40})\s*:', re.MULTILINE)

    result = []
    for sc in scenes:
        text = sc['text']
        # Skip the trailing BLOCKING_END payload — it sometimes lists all 6 figs as a stage diagram
        # which inflates the count. We want only spoken/active beats in the actual scene body.
        body = text
        # Drop [BLOCKING_END] ... fence content from the count (it's prod blocking, not dialogue)
        body = re.sub(r'\[BLOCKING_END\].*?(?=\n\s*\n|\Z)', '', body, flags=re.DOTALL)
        # Also drop [BLOCKING] ... fence (only initial stage notes — but actually these DO list
        # characters who will be in the scene with actions. So we count them too, but ONLY
        # those who also have a dialogue or an action elsewhere in the body OR are listed in BLOCKING.)
        speakers = set()
        for m in dialogue_line_re.finditer(body):
            name = m.group(1).strip().rstrip('.').strip()
            # Match against canonical cast — exact or case-insensitive
            for cn in cast_set:
                if cn.lower() == name.lower():
                    speakers.add(cn)
                    break
                # Also handle "DR. FINCH" matching "Dr. Harold Finch" — prefix match on lowercase
                if cn.lower().startswith(name.lower()) and len(name) >= 4:
                    speakers.add(cn)
                    break
        # Also pick up characters mentioned in BLOCKING with explicit actions (sits / stands / etc.)
        blocking_re = re.compile(r'\[BLOCKING\](.*?)\[/BLOCKING\]', re.DOTALL | re.IGNORECASE)
        for bm in blocking_re.finditer(text):
            block_text = bm.group(1)
            # Each line: NAME: action :: OUTFIT: ...
            for line in block_text.splitlines():
                ml = re.match(r'\s*([A-ZА-ЯЁ][A-ZA-Zа-яёА-ЯЁ\.\-\' ]{1,40})\s*:', line)
                if ml:
                    name = ml.group(1).strip().rstrip('.').strip()
                    if name.lower() in ('location', 'outfit', 'outfit_desc'):
                        continue
                    for cn in cast_set:
                        if cn.lower() == name.lower():
                            speakers.add(cn)
                            break
                        if cn.lower().startswith(name.lower()) and len(name) >= 4:
                            speakers.add(cn)
                            break

        result.append({
            'idx': sc['idx'],
            'location': sc['location'],
            'characters': sorted(speakers),
            'count': len(speakers),
        })
    return result


def detect_scene_overcrowding(s, script: str) -> list[dict]:
    """Return list of scene-overcrowding violations relative to series' max_main_chars_per_scene.
    Each violation: {scene_idx, location, count, limit, characters}.
    Empty list = no violations or no limit configured.
    """
    try:
        limit = int(s.get('max_main_chars_per_scene') or 0)
    except (TypeError, ValueError):
        return []
    if limit < 1 or limit > 6:
        return []
    cast_names = [(c.get('name') or '').strip() for c in (s.get('characters') or [])
                  if (c.get('name') or '').strip()]
    if not cast_names:
        return []
    scenes = _count_speaking_characters_per_scene(script, cast_names)
    violations = []
    for sc in scenes:
        if sc['count'] > limit:
            violations.append({
                'scene_idx': sc['idx'],
                'location': sc['location'],
                'count': sc['count'],
                'limit': limit,
                'characters': sc['characters'],
            })
    return violations


# ── No-name-character detector ───────────────────────────────────────────────
# Every on-camera / speaking character MUST carry a UNIQUE PROPER NAME and a
# cast-block line so its reference portrait binds reliably at generation time.
# Bare role labels ("CLIENT", "OLD WOMAN", "MAN #2") never get a stable ref →
# the model renders the wrong face (the recurring «героиня вместо клиента» bug,
# e.g. «My Sister Owns the Nail Salon» ep 3 — CLIENT cue, no Mrs. Park ref).
_GENERIC_ROLE_WORDS = {
    # English roles
    'client','customer','patient','doctor','nurse','waiter','waitress','clerk',
    'bartender','reporter','journalist','bodyguard','guard','receptionist','driver',
    'cop','officer','detective','manager','boss','teacher','student','maid','butler',
    'cashier','barista','salesman','saleswoman','secretary','assistant','agent','soldier',
    'guy','lady','gentleman','stranger','neighbor','neighbour','passenger','pedestrian',
    'man','woman','boy','girl','kid','child','teen','teenager','baby','infant','toddler',
    'mother','father','mom','dad','son','daughter','sister','brother','husband','wife',
    'friend','colleague','coworker','crowd','people','men','women','person','someone',
    'host','hostess','chef','cook','janitor','plumber','mechanic','vendor','seller','buyer',
    'lawyer','witness','victim','suspect','intern','employee','worker','owner','landlord',
    # Russian roles
    'клиент','клиентка','покупатель','покупательница','пациент','пациентка','врач','доктор',
    'медсестра','медбрат','официант','официантка','охранник','водитель','мужчина','женщина',
    'девушка','девочка','парень','мальчик','ребёнок','ребенок','незнакомец','незнакомка',
    'сосед','соседка','полицейский','детектив','менеджер','начальник','начальница','учитель',
    'учительница','ученик','ученица','прохожий','пассажир','толпа','люди','человек',
    'мать','отец','мама','папа','сын','дочь','сестра','брат','муж','жена','друг','подруга',
    'адвокат','свидетель','свидетельница','жертва','подозреваемый','владелец','сотрудник',
}
# Titles that legitimately PREFIX a real name (Mrs. Park, Dr. Harris).
_NAME_TITLES = {
    'mr','mrs','ms','miss','dr','sir','lord','captain','colonel','sergeant','professor',
    'prof','madam','madame','aunt','uncle','grandma','grandpa','мистер','миссис','мисс',
    'капитан','полковник','профессор','тётя','тетя','дядя','бабушка','дедушка',
}
# Determiners / adjectives that do not constitute a proper name on their own.
_NAME_FILLER = {
    'the','a','an','another','other','old','young','elderly','tall','short','fat','thin',
    'mysterious','strange','angry','random','unknown','new','big','small','little','first',
    'second','third','тот','та','этот','эта','старый','старая','молодой','молодая','новый',
    'странный','неизвестный','первый','второй','другой','один','одна',
}

def _label_is_unnamed(label: str) -> bool:
    """True if a speaker cue / BLOCKING name is a GENERIC role label carrying no
    proper name. 'CLIENT' / 'OLD WOMAN' / 'MAN #2' / 'THE GUY' → True.
    'MRS. PARK' / 'DR. HARRIS' / 'NURSE BROOKS' / 'MAI' → False."""
    if not label:
        return False
    saw_role = False
    name_toks = []
    for raw in re.split(r'[\s\.\-]+', label.lower()):
        t = re.sub(r'[^0-9a-zа-яё]', '', raw)
        if not t or t.isdigit():
            continue
        if t in _GENERIC_ROLE_WORDS:
            saw_role = True
            continue
        if t in _NAME_TITLES or t in _NAME_FILLER or len(t) < 2:
            continue
        name_toks.append(t)
    return saw_role and not name_toks


def _extract_cast_block_names(script: str) -> set:
    """Names declared in the script's own === EPISODE CAST === block."""
    names = set()
    m = re.search(r'=== EPISODE CAST ===(.*?)=== END CAST ===', script or '', re.DOTALL)
    if not m:
        return names
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.upper().startswith('CHARACTER:'):
            continue
        seg = line.split('|', 1)[0]
        if ':' in seg:
            nm = re.sub(r'\s*\(.*?\)\s*$', '', seg.split(':', 1)[1]).strip()
            if nm:
                names.add(nm)
    return names


def _extract_speaker_and_blocking_labels(script: str) -> list:
    """Every dialogue cue + [BLOCKING] entry as (label, kind). The cast block is
    stripped first so its CHARACTER: lines aren't mistaken for cues."""
    if not script:
        return []
    out = []
    body = re.sub(r'=== EPISODE CAST ===.*?=== END CAST ===', '', script, flags=re.DOTALL)
    SKIP = {'location','outfit','outfit_desc','outfitdesc','int','ext','инт','экст','инта',
            'кратко','note','notes','reversal','blocking','blocking_end','time','gender',
            'look','role','is_base','день','ночь','утро','вечер','character'}
    cue_re = re.compile(
        r"^[ \t>*_]*([A-ZА-ЯЁ][A-ZА-ЯЁ0-9 \-\.'#]{1,30})\s*(?:\*?\([^)\n]+\)\*?)?\s*:",
        re.MULTILINE,
    )
    for m in cue_re.finditer(body):
        lab = m.group(1).strip().rstrip('.').strip()
        if lab and lab.lower() not in SKIP:
            out.append((lab, 'cue'))
    for bm in re.finditer(r'\[BLOCKING(?:_END)?\](.*?)\[/BLOCKING(?:_END)?\]',
                          script, re.DOTALL | re.IGNORECASE):
        for line in bm.group(1).splitlines():
            ml = re.match(r"\s*([A-ZА-ЯЁ][A-ZА-ЯЁ0-9 \-\.']{1,30})\s*:", line)
            if not ml:
                continue
            lab = ml.group(1).strip().rstrip('.').strip()
            if lab and lab.lower() not in SKIP:
                out.append((lab, 'blocking'))
    return out


def detect_unnamed_characters(s, script: str) -> list:
    """Programmatic write-time guard. Flags (critical) any on-camera character
    that won't bind a reference portrait:
      • unnamed_character — bare generic role cue/BLOCKING ("CLIENT", "OLD WOMAN").
      • uncast_character — a NAMED speaker/BLOCKING entry with no matching line in
        === EPISODE CAST === (and not a known series character) → no card built.
    Returns list of violation dicts (same shape the audit loop consumes)."""
    if not script:
        return []
    cast_names = _extract_cast_block_names(script)
    series_names = {(c.get('name') or '').strip()
                    for c in (s.get('characters') or []) if (c.get('name') or '').strip()}
    all_names = cast_names | series_names
    known_lower = {n.lower() for n in all_names if n}
    resolver_roster = [{'name': n} for n in all_names]

    unnamed, uncast = {}, {}
    for lab, kind in _extract_speaker_and_blocking_labels(script):
        if _label_is_unnamed(lab):
            unnamed.setdefault(lab, set()).add(kind)
            continue
        # Named label — must resolve to a cast / series character so a card exists.
        if lab.lower() in known_lower:
            continue
        if _resolve_char_by_script_name(lab, resolver_roster) is not None:
            continue
        uncast.setdefault(lab, set()).add(kind)

    violations = []
    for lab, kinds in sorted(unnamed.items()):
        violations.append({
            'type': 'unnamed_character',
            'severity': 'critical',
            'where': f'«{lab}» ({"/".join(sorted(kinds))})',
            'explanation': (
                f'Персонаж обозначен безымянной ролью «{lab}» — нет уникального имени '
                f'и карточки. При генерации он НЕ получает референс-портрет, и модель '
                f'рисует на его месте чужое лицо (частый баг: вместо клиента в кадре '
                f'появляется главная героиня).'
            ),
            'fix': (
                f'Дай этому персонажу УНИКАЛЬНОЕ собственное имя (напр. «MRS. PARK», '
                f'«DR. HARRIS», «OLD TOM») и используй ОДНУ И ТУ ЖЕ строку ВЕЗДЕ: в '
                f'=== EPISODE CAST === (CHARACTER: ИМЯ | GENDER | LOOK | OUTFIT | IS_BASE: true), '
                f'в кью-реплике (ИМЯ:), в [BLOCKING] и в описаниях действия. Никаких голых '
                f'ролей CLIENT/WAITER/MAN/WOMAN как идентификатора. Безымянными остаются '
                f'только молчаливые фоновые статисты без реплик и без [BLOCKING] — их '
                f'упоминай только в прозе.'
            ),
        })
    for lab, kinds in sorted(uncast.items()):
        violations.append({
            'type': 'uncast_character',
            'severity': 'critical',
            'where': f'«{lab}» ({"/".join(sorted(kinds))})',
            'explanation': (
                f'Персонаж «{lab}» говорит/присутствует в кадре, но его НЕТ в блоке '
                f'=== EPISODE CAST === — карточка не создастся и референс не прикрепится.'
            ),
            'fix': (
                f'Добавь строку в === EPISODE CAST ===: CHARACTER: {lab} | GENDER: … | '
                f'LOOK: … | OUTFIT: … | IS_BASE: true. Имя в касте должно ПОБУКВЕННО '
                f'совпадать с кью-репликой и [BLOCKING].'
            ),
        })
    return violations


def _script_runtime_metrics(script: str) -> dict:
    """Programmatic length scan — counts dialogue lines + spoken words + action lines
    + estimated runtime.

    Returns:
        dialogue_lines, dialogue_words, action_lines, longest_line_words, avg_line_words,
        est_runtime_sec.

    Heuristic (calibrated against real TikTok/Reels short drama timings):
      - Spoken delivery: ~150 wpm for plain rapid dialogue, dropping to ~120 wpm for
        long emotional beats. We use 135 wpm midpoint.
      - Each dialogue beat: +1.2s baseline for the actor's "settle" + reaction
        (longer beats need more visual support, so we add another +0.8s per beat
        with >8 words).
      - Each action line (non-dialogue narrative outside [BLOCKING]): +2.0s — actions
        like "She picks up the phone" or "He walks across the room" take real screen
        time even with no words spoken.
      - Looks for explicit time markers in action lines ("две минуты молча", "for ten
        seconds") and adds them — writers sometimes script multi-minute beats in a
        single sentence.
    """
    if not script:
        return {
            'dialogue_lines': 0, 'dialogue_words': 0, 'action_lines': 0,
            'longest_line_words': 0, 'avg_line_words': 0, 'est_runtime_sec': 0,
        }
    # Speaker cue. CRITICAL: must accept parenthetical cues like
    #   DEREK (O.S.):  /  JESSICA (V.O.):  /  ETHAN (CONT'D):  /  MAYA (тихо):
    # Before 2026-06-04 the char-class excluded '(' ')' so EVERY `NAME (O.S.):`
    # line fell through to "action line" — the detector saw 3 dialogue lines +
    # 17 action lines in a normal 8-line dialogue scene, undercounting spoken
    # words ~75% and overcounting action. That made the length governor fire
    # contradictory violations (too few words AND too much action at once),
    # broke the retry loop, and shipped wildly inconsistent episode lengths.
    dialogue_re = re.compile(
        r'^\s*[A-ZА-ЯЁ][A-ZA-Zа-яёА-ЯЁ0-9\.\-\' ]{0,40}(?:\([^)]*\))?\s*:\s*(.*)$')
    # Skip lines inside [BLOCKING]/[BLOCKING_END] / scene headers / cut-markers
    scene_header_re = re.compile(r'^\s*(?:INT\.|EXT\.|ИНТ\.|ЭКСТ\.|INT/EXT\.)', re.IGNORECASE)
    cut_marker_re   = re.compile(r'═══\s*END\s+EPISODE', re.IGNORECASE)
    # Time markers in narrative — "две минуты молча", "for 30 seconds", "10 секунд"
    minute_re = re.compile(r'\b(\d+|одну?|две|три|четыре|пять|десять|fifteen|twenty|thirty)\s*(?:минут|minutes|min)\b', re.IGNORECASE)
    second_re = re.compile(r'\b(\d+|десять|fifteen|twenty|thirty)\s*(?:секунд|seconds|sec)\b', re.IGNORECASE)
    _word_to_num = {'one':1,'two':2,'three':3,'four':4,'five':5,'ten':10,'fifteen':15,'twenty':20,'thirty':30,
                    'одну':1,'один':1,'две':2,'два':2,'три':3,'четыре':4,'пять':5,'десять':10}

    # Parenthetical stage directions inside a dialogue line — e.g.
    # `ETHAN: (в микрофон, указывая на Кайна) Viktor Kain laundered three…`
    # The parenthetical is stage direction, NOT spoken text — strip before
    # counting words. Real bug 2026-05-30: «I Became My Dead Brother's Ghost»
    # ep 1 reported 91 spoken words; user said «по факту персонажи говорят
    # около 50 слов». Difference was 100% explained by parentheticals being
    # counted as speech.
    paren_re = re.compile(r'\([^)]*\)')
    in_blocking = False
    in_dialogue_continuation = False  # for multi-line dialogue (NAME:\n"line")
    current_speaker = None
    dialogue_lines = 0
    dialogue_words = 0
    action_lines = 0
    longest_line_words = 0
    explicit_time_sec = 0
    line_word_counts = []

    lines = script.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        # Toggle BLOCKING fences
        if '[BLOCKING]' in raw.upper() and '[/BLOCKING]' not in raw.upper():
            in_blocking = True
            continue
        if '[/BLOCKING]' in raw.upper() or '[BLOCKING_END]' in raw.upper():
            in_blocking = False
            continue
        if in_blocking:
            continue
        if not line:
            in_dialogue_continuation = False
            continue
        if scene_header_re.match(raw) or cut_marker_re.search(raw):
            in_dialogue_continuation = False
            continue
        if line.startswith('Кратко:') or line.startswith('Episode '):
            continue
        # Dialogue header (NAME:)
        m = dialogue_re.match(raw)
        if m:
            current_speaker = True
            tail = m.group(1).strip()
            # Strip parenthetical stage directions — they're NOT spoken words.
            tail_spoken = paren_re.sub('', tail).strip()
            if tail_spoken:
                # Inline dialogue: NAME: text
                wc = len(tail_spoken.split())
                dialogue_lines += 1
                dialogue_words += wc
                line_word_counts.append(wc)
                if wc > longest_line_words:
                    longest_line_words = wc
                in_dialogue_continuation = False
            elif tail:
                # Header had ONLY a parenthetical (e.g. `ETHAN: (whispers)`) —
                # the spoken text comes on the next line.
                in_dialogue_continuation = True
            else:
                # Header on its own — next non-empty line is the actual dialogue
                in_dialogue_continuation = True
            continue
        # Continuation of a dialogue header (multi-line: NAME:\n"text")
        if in_dialogue_continuation:
            # Strip surrounding quotes + parenthetical stage directions
            tail = line.strip('"').strip("'").strip('«»').strip()
            tail = paren_re.sub('', tail).strip()
            if tail:
                wc = len(tail.split())
                dialogue_lines += 1
                dialogue_words += wc
                line_word_counts.append(wc)
                if wc > longest_line_words:
                    longest_line_words = wc
            in_dialogue_continuation = False
            continue
        # Otherwise — action / narrative line
        action_lines += 1
        # Scan for explicit time markers
        for mm in minute_re.finditer(line):
            raw_v = mm.group(1).lower()
            n = int(raw_v) if raw_v.isdigit() else _word_to_num.get(raw_v, 0)
            explicit_time_sec += n * 60
        for mm in second_re.finditer(line):
            raw_v = mm.group(1).lower()
            n = int(raw_v) if raw_v.isdigit() else _word_to_num.get(raw_v, 0)
            explicit_time_sec += n

    avg_line = round(dialogue_words / dialogue_lines, 1) if dialogue_lines else 0

    # Runtime estimate — CALIBRATED TO THE JS SEGMENTER (static/app.js), which is
    # the ground truth for actual rendered video length. The segmenter packs the
    # script into Seedance chunks using SPEECH_WPS=2.65 and ACTION_BEAT_SEC=1.5,
    # then each chunk carries a ~1.5s buffer. If THIS estimator uses a different
    # calibration (it used 135wpm≈2.25wps + 2.0s/action before 2026-06-04) the
    # writer is told "60s" by one yardstick while the renderer produces ~45s —
    # which is exactly why 60s-target episodes came out at 30/40/60s.
    _SPEECH_WPS = 2.65          # mirror SPEECH_WPS in app.js
    _ACTION_BEAT_SEC = 1.5      # mirror ACTION_BEAT_SEC in app.js
    _LINE_PREPAUSE = 0.4        # mirror per-line 0.4s pre-pause in _lineDuration
    _CHUNK_BUFFER = 1.5         # mirror per-chunk buffer in _estimateChunkDurationSec
    _CHUNK_SEC = 14.0           # mirror effective chunk packing size

    spoken_sec = dialogue_words / _SPEECH_WPS
    beat_overhead = _LINE_PREPAUSE * len(line_word_counts)
    action_sec = action_lines * _ACTION_BEAT_SEC
    # Cap explicit_time_sec at 120s to avoid runaway from typos like "100 минут"
    explicit_time_sec = min(explicit_time_sec, 120)

    content_sec = spoken_sec + beat_overhead + action_sec + explicit_time_sec
    # Per-chunk buffer: the renderer splits content into ~14s chunks, each padded.
    import math as _math
    num_chunks = max(1, _math.ceil(content_sec / _CHUNK_SEC)) if content_sec > 0 else 0
    est_runtime = round(content_sec + num_chunks * _CHUNK_BUFFER)

    return {
        'dialogue_lines': dialogue_lines,
        'dialogue_words': dialogue_words,
        'action_lines': action_lines,
        'longest_line_words': longest_line_words,
        'avg_line_words': avg_line,
        'est_runtime_sec': est_runtime,
        'explicit_time_sec': explicit_time_sec,
    }


def detect_script_overlength(s, script: str) -> dict:
    """Programmatic length-budget detector. Returns {} (within budget) or a violation dict.

    Target duration comes from `target_duration_sec` on the series (default 60s).
    Triggers (any of):
      • estimated runtime > 130% of target (script too long)
      • any single dialogue line > 12 words (long monologues kill TikTok pacing)
      • avg dialogue line > 9 words (overall too talky)
      • action line count > 1.5× action budget (too much narrative business)
      • dialogue_words < 60% of target — UNDERSHOOT. Writer is too cautious and
        delivers half the spoken-words target; the resulting video has long
        silent stretches because the chunker still produces the BLOCKING-driven
        scenes but the audio runs out. Catches the 2026-05-30 issue where a
        ~100-word target produced ~50 spoken words.
    """
    try:
        target_sec = int((s or {}).get('target_duration_sec') or 60)
    except (TypeError, ValueError):
        target_sec = 60
    # Speech budget @2.65 wps (matches JS segmenter): ~110 words fills a 60s
    # episode once per-line pre-pauses, action beats and chunk buffers are added.
    target_words = round(target_sec / 60 * 110)
    target_lines = max(3, min(40, round(target_sec / 4.5)))
    # SYMMETRIC band — the whole point of this detector is that a 60s target
    # produces ~60s, not 30/40/60. Both ends are enforced so the writer can't
    # under- OR over-shoot. Floor 0.8×, ceiling 1.2× of target runtime.
    floor_sec = target_sec * 0.8
    ceil_sec = target_sec * 1.2
    floor_words = round(target_words * 0.75)
    m = _script_runtime_metrics(script)
    est_sec = m['est_runtime_sec']
    dialogue_lines = m['dialogue_lines']
    dialogue_words = m['dialogue_words']
    longest_line = m['longest_line_words']
    avg_line = m['avg_line_words']
    action_lines = m['action_lines']

    reasons = []
    if est_sec > ceil_sec:
        reasons.append(
            f"runtime ~{est_sec}с — СЛИШКОМ ДЛИННО ({round(est_sec/target_sec, 1)}× от {target_sec}с). "
            f"Сократи реплики/action до ~{target_sec}с (потолок {round(ceil_sec)}с).")
    elif est_sec < floor_sec and est_sec > 0:
        reasons.append(
            f"runtime ~{est_sec}с — СЛИШКОМ КОРОТКО (нужно ~{target_sec}с, минимум {round(floor_sec)}с). "
            f"Добавь реплик/действий до ~{target_sec}с. Серия выйдет короче заявленной длины.")
    if longest_line > 12:
        reasons.append(f"самая длинная реплика {longest_line} слов (лимит 10, идеал 3-7)")
    if avg_line > 9:
        reasons.append(f"средняя длина реплики {avg_line} слов (лимит 7, идеал 5)")
    # Action lines budget proportional to target duration
    action_budget = max(3, round(target_sec / 12))
    if action_lines > action_budget * 1.5:
        reasons.append(f"action-строк {action_lines} (лимит ~{action_budget})")
    # Words floor only fires when runtime didn't already flag undershoot (avoid
    # double-reporting the same problem).
    if dialogue_words < floor_words and est_sec >= floor_sec:
        reasons.append(
            f"спикерских слов {dialogue_words} — мало (минимум {floor_words}, цель {target_words}). "
            f"ACTION/BLOCKING не считаются. Сцена выйдет полупустой."
        )

    if not reasons:
        return {}
    return {
        'target_sec': target_sec,
        'target_lines': target_lines,
        'target_words': target_words,
        'est_sec': est_sec,
        'dialogue_lines': dialogue_lines,
        'dialogue_words': dialogue_words,
        'longest_line_words': longest_line,
        'avg_line_words': avg_line,
        'action_lines': action_lines,
        'ratio': round(est_sec / target_sec, 1),
        'reasons': reasons,
    }


def _build_narrative_state_block(sid: str, num: int) -> str:
    """Build the narrative momentum context block for episode num's generation prompt.
    Returns a POSITIVE DIRECTIVE block (not a prohibition list) showing the story trajectory
    and what this episode must achieve. Empty string if insufficient data.
    """
    s = load_series(sid)
    if s is None:
        return ''
    index = s.get('narrative_index') or []
    # Only use episodes before current num
    prior = [e for e in index if e.get('ep', 0) < num]
    if len(prior) < 2:
        return ''  # Not enough history yet

    window = prior[-8:]  # Last 8 episodes

    # Build history lines
    history_lines = []
    for e in window:
        ep_num = e.get('ep', '?')
        arch   = e.get('archetype', '?')
        pd     = e.get('power_delta', '?')
        em     = e.get('closing_emotion', '?')
        am     = e.get('antagonist_momentum', '?')
        history_lines.append(f"  Ep{ep_num}: {arch} → power: {pd} → mood: {em} | antagonist: {am}")

    # Detect patterns in last 6 episodes
    last6 = prior[-6:]
    arch_counts     = Counter(e.get('archetype') for e in last6 if e.get('archetype'))
    power_counts    = Counter(e.get('power_delta') for e in last6 if e.get('power_delta'))
    emotion_counts  = Counter(e.get('closing_emotion') for e in last6 if e.get('closing_emotion'))
    ant_counts      = Counter(e.get('antagonist_momentum') for e in last6 if e.get('antagonist_momentum'))

    # Device history — keep as secondary signal
    dev_index = s.get('devices_index') or {}
    device_warnings = []
    for dev_id in DEVICE_TAXONOMY:
        entries = dev_index.get(dev_id, [])
        if len(entries) >= 3:
            last_ep = max(e.get('ep', 0) for e in entries)
            device_warnings.append(f"  • {dev_id} — использован {len(entries)} раз (последний: ep{last_ep}) — ИСЧЕРПАН нарративно")

    # Build pattern warnings
    pattern_alerts = []
    overused_archs = [a for a, n in arch_counts.items() if n >= 3]
    if overused_archs:
        pattern_alerts.append(f"⚠️  Archetype '{overused_archs[0]}' — {arch_counts[overused_archs[0]]} из последних 6 серий")

    ant_losing_streak = power_counts.get('antagonist_wins', 0)
    if ant_losing_streak >= 4:
        pattern_alerts.append(f"⚠️  Протагонист проигрывает {ant_losing_streak} из последних 6 серий — зритель потерял веру")

    prot_winning_streak = power_counts.get('protagonist_wins', 0)
    if prot_winning_streak >= 4:
        pattern_alerts.append(f"⚠️  Протагонист побеждает {prot_winning_streak} из последних 6 — нужен серьёзный setback")

    dominant_emotion = emotion_counts.most_common(1)
    if dominant_emotion and dominant_emotion[0][1] >= 4:
        pattern_alerts.append(f"⚠️  Финальная эмоция '{dominant_emotion[0][0]}' повторяется {dominant_emotion[0][1]} раз — зритель привыкает")

    # Build REQUIRED directives
    directives = []
    if overused_archs:
        forbidden_arch_str = ', '.join(overused_archs)
        allowed_archs = [a for a in NARRATIVE_ARCHETYPES if a not in overused_archs]
        directives.append(f"→ archetype: НЕ '{forbidden_arch_str}' — выбери из: {', '.join(allowed_archs[:4])}")
    if ant_losing_streak >= 4:
        directives.append(f"→ power_delta: ДОЛЖЕН БЫТЬ protagonist_wins — протагонист добивается РЕАЛЬНОЙ победы, не просто 'узнаёт что-то'")
    if dominant_emotion and dominant_emotion[0][1] >= 4:
        bad_em = dominant_emotion[0][0]
        other_ems = [e for e in NARRATIVE_EMOTIONS if e != bad_em]
        directives.append(f"→ closing_emotion: НЕ '{bad_em}' снова — целься в: {', '.join(other_ems[:3])}")
    if ant_counts.get('escalating', 0) >= 4:
        directives.append(f"→ antagonist: должен впервые столкнуться с серьёзным препятствием, ошибкой или неожиданным осложнением")
    if device_warnings:
        directives.append(f"→ info-delivery: НЕ использовать исчерпанные механизмы (см. список ниже)")

    if not pattern_alerts and not directives:
        # Story has good variety — just show history as context, no hard directives
        block = (
            '═══ NARRATIVE MOMENTUM (последние серии) ═══\n'
            + '\n'.join(history_lines)
            + '\n✓ Хорошее разнообразие — продолжай варьировать archetype и emotional close.\n'
            '═══════════════════════════════════════════════════\n\n'
        )
        return block

    # Build the full block
    lines = ['═══ NARRATIVE MOMENTUM — ИСТОРИЯ КАК ЕЁ ВИДИТ ЗРИТЕЛЬ ═══']
    lines.append('Последние серии:')
    lines.extend(history_lines)
    if pattern_alerts:
        lines.append('')
        lines.extend(pattern_alerts)
    lines.append('')
    lines.append('════ ЭТОТ ЭПИЗОД ДОЛЖЕН ════')
    if directives:
        lines.extend(directives)
    else:
        lines.append('→ Поддержи хорошее разнообразие — не повторяй ни archetype, ни emotional close прошлой серии')
    if device_warnings:
        lines.append('')
        lines.append('Исчерпанные info-delivery механизмы (не использовать):')
        lines.extend(device_warnings)
    lines.append('')
    lines.append('ГЛАВНОЕ: каждый эпизод должен заканчиваться в ДРУГОМ эмоциональном состоянии, чем предыдущий.')
    lines.append('Разнообразие — это не смена декораций, это смена того, кто побеждает и что зритель чувствует.')
    lines.append('═══════════════════════════════════════════════════')
    lines.append('')

    return '\n'.join(lines)


def build_logic_brief(sid, num):
    """Pre-write: produce a constraints brief for episode `num` from canon + recent context."""
    s = load_series(sid)
    if not s: return ''
    canon = load_canon(sid)
    ep = load_episode(sid, num) or {}

    canon_block = _format_canon_for_prompt(canon)
    rules_block = json.dumps(WORLD_RULES, ensure_ascii=False, indent=2)

    # Recent episodes context (last 2 synopses + last script tail)
    recent = []
    for n in range(max(1, num - 2), num):
        prev = load_episode(sid, n)
        if prev:
            recent.append(f"Ep{n} synopsis: {prev.get('synopsis','')[:300]}")

    requested_day_advance = ep.get('days_since_previous')
    advance_hint = (f"\nPLANNED time skip from previous episode: {requested_day_advance} day(s)."
                    if requested_day_advance else
                    "\nNo planned time skip specified — infer minimum needed for biology/logic.")

    prompt = (
        f'Series: "{s.get("title","")}" | Genre: {s.get("genre","")}\n'
        f'Synopsis of THIS episode (ep {num}): {ep.get("synopsis","")}\n'
        + advance_hint + '\n\n'
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== RECENT EPISODES ===\n' + ('\n'.join(recent) or '—') + '\n\n'
        f'=== WORLD RULES (real-world constants) ===\n{rules_block}\n\n'
        'Produce a constraints brief in RUSSIAN with these sections (use exactly these headers):\n'
        '## TIMELINE\n  - на каком дне происходит серия, сколько прошло с предыдущей, проверка биологических окон\n'
        '## LOCKED FACTS IN PLAY\n  - какие факты из канона активны в этой серии и как их соблюсти\n'
        '## CHARACTER KNOWLEDGE — ЧТО МОЖНО / НЕЛЬЗЯ ГОВОРИТЬ\n  - для каждого персонажа в серии: что он знает, что НЕ может знать (запрещённые реплики)\n'
        '## REQUIRED REVERSAL ANCHOR\n  - какой канонический факт реверсал может перевернуть, чтобы не вводить новый ретконн\n'
        '## OPEN THREADS TO ADDRESS\n  - какие открытые вопросы серия ОБЯЗАНА закрыть или явно отложить\n'
        '## FORBIDDEN CONTRADICTIONS\n  - короткий список конкретных вещей, которые сломают канон если появятся\n'
        '## BIOLOGY/PHYSICS CHECKS\n  - применимые числовые ограничения из WORLD RULES для этой серии\n'
        'Будь предельно конкретным. Если поле пустое — напиши "—". Не больше 25 строк всего.'
    )
    try:
        brief = claude_ask_fast(prompt, system=_BRIEF_SYSTEM).strip()
    except Exception as e:
        brief = f'(logic brief generation failed: {e})'
    # Append format-mode block (short_drama vs instagram_series) — top of brief
    try:
        fmt_block = _format_mode_block(s, sections=['episode_rule', 'pace_rule'])
        if fmt_block:
            brief = f'## FORMAT MODE\n{fmt_block}\n' + brief
    except Exception:
        pass
    # Prepend user-pinned story trajectory (finale + checkpoints) — this is the
    # signal that the auditor and the writer both need to honor. Without it
    # surfaced at the top of the brief, the auditor cannot flag finale drift.
    try:
        traj = build_trajectory_block(s, num)
        if traj:
            brief = f'## STORY TRAJECTORY (USER-PINNED LANDMARKS)\n{traj}\n' + brief
    except Exception:
        pass
    # Append scene character cap rule (hard production constraint)
    try:
        crowd_rule = _build_crowd_constraint_block(s)
        if crowd_rule:
            brief += f'\n\n## ЛИМИТ ПЕРСОНАЖЕЙ В СЦЕНЕ (HARD RULE)\n{crowd_rule}'
    except Exception:
        pass
    # Append narrative momentum context to brief
    try:
        narrative_block = _build_narrative_state_block(sid, num)
        if narrative_block:
            brief += f'\n\n## NARRATIVE MOMENTUM — VARIETY REQUIRED\n{narrative_block}'
    except Exception:
        pass
    # Append plot-device forbidden list if present (secondary signal)
    try:
        devices_block = _build_plot_device_history(sid, num)
        if devices_block:
            brief += f'\n\n## ИСЧЕРПАННЫЕ INFO-DELIVERY МЕХАНИЗМЫ\n{devices_block}'
    except Exception:
        pass
    return brief


def audit_script(sid, num, script, brief):
    """Post-write: returns dict {passes, violations}. Never raises."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== CANON ===\n{canon_block}\n\n'
        f'=== LOGIC BRIEF FOR EP {num} ===\n{brief}\n\n'
        f'=== SCRIPT TO AUDIT ===\n{script}\n\n'
        'Find every continuity violation. Output strict JSON per the schema.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_AUDIT_SYSTEM)
        data = loads_lenient(strip_json(raw))
        if not isinstance(data, dict): raise ValueError('not a dict')
        data.setdefault('violations', [])
        data['passes'] = bool(data.get('passes', not any(
            v.get('severity') == 'critical' for v in data['violations']
        )))
        return data
    except Exception as e:
        # Last-ditch repair: try just regex-stripping fences + lenient parse
        try:
            data = loads_lenient(_strip_markdown_fence(raw))
            if isinstance(data, dict):
                data.setdefault('violations', [])
                data['passes'] = bool(data.get('passes', not any(
                    v.get('severity') == 'critical' for v in data['violations']
                )))
                return data
        except Exception:
            pass
        _log_event('WARN', 'audit_json_parse_fail', err=str(e)[:200], raw_head=raw[:200] if 'raw' in dir() else '')
        return {'passes': True, 'violations': [], 'audit_error': str(e)}


def extract_canon_updates(sid, num, script):
    """Auto-extract canon updates from a finalized script and merge into canon.json."""
    canon = load_canon(sid)
    canon_block = _format_canon_for_prompt(canon)
    prompt = (
        f'=== EXISTING CANON (for context, do NOT repeat existing facts) ===\n{canon_block}\n\n'
        f'=== EPISODE {num} SCRIPT ===\n{script}\n\n'
        'Extract canon updates per the JSON schema. Only NEW information from THIS episode.'
    )
    try:
        raw = claude_ask_fast(prompt, system=_EXTRACT_SYSTEM)
        upd = json.loads(strip_json(raw))
    except Exception as e:
        return {'error': str(e)}

    # Merge into canon
    wc = canon['world_clock']
    advance = max(0, int(upd.get('world_day_advance') or 0))
    # If episode N replaces a previously recorded one, recompute from previous tl entry
    prev_day = wc.get('current_day', 0)
    new_day = prev_day + (advance if num > wc.get('last_episode', 0) else 0)
    wc['current_day'] = new_day
    wc['last_episode'] = max(wc.get('last_episode', 0), num)

    # Timeline (replace any existing entry for this ep)
    canon['timeline'] = [t for t in canon.get('timeline', []) if t.get('ep') != num]
    canon['timeline'].append({
        'ep': num, 'day': new_day, 'events': upd.get('events', [])[:6]
    })
    canon['timeline'].sort(key=lambda t: t.get('ep', 0))

    # New facts
    fact_id_map = {}
    for nf in upd.get('new_facts', []):
        fid = _next_id('F', canon['facts'])
        canon['facts'].append({
            'id': fid, 'ep': num,
            'fact': nf.get('fact', '')[:240],
            'locked': True,
            'supersedes': nf.get('supersedes') or None,
        })

    # Character state
    cs = canon.setdefault('character_state', {})
    for name, ch_upd in (upd.get('character_updates') or {}).items():
        st = cs.setdefault(name, {'knows': [], 'suspects': [], 'physical': {}, 'location': None})
        # store learned facts as inline strings prefixed with episode (cheap, no fact-id matching)
        for learned in (ch_upd.get('learned') or [])[:5]:
            tag = f'ep{num}: {learned}'[:120]
            if tag not in st['knows']:
                st['knows'].append(tag)
        if ch_upd.get('physical'):
            st.setdefault('physical', {}).update(ch_upd['physical'])
        if ch_upd.get('location'):
            st['location'] = ch_upd['location']

    # Threads
    for t_open in upd.get('threads_opened', []):
        tid = _next_id('T', canon['open_threads'])
        canon['open_threads'].append({
            'id': tid, 'opened_ep': num,
            'question': t_open.get('question', '')[:240],
            'status': 'open',
        })
    closed_ids = set(upd.get('threads_closed') or [])
    for t in canon['open_threads']:
        if t.get('id') in closed_ids and t.get('status') != 'closed':
            t['status'] = 'closed'
            t['resolved_ep'] = num

    save_canon(sid, canon)
    return {'ok': True, 'world_day': new_day, 'new_facts': len(upd.get('new_facts', []))}


def rollback_canon_for_episode(sid, num):
    """Remove all canon entries created by episode `num` (used before regenerating)."""
    canon = load_canon(sid)
    canon['facts'] = [f for f in canon['facts'] if f.get('ep') != num]
    canon['timeline'] = [t for t in canon['timeline'] if t.get('ep') != num]
    canon['open_threads'] = [t for t in canon['open_threads'] if t.get('opened_ep') != num]
    for t in canon['open_threads']:
        if t.get('resolved_ep') == num:
            t.pop('resolved_ep', None)
            t['status'] = 'open'
    # Roll back character knowledge tagged with ep
    for name, st in (canon.get('character_state') or {}).items():
        st['knows'] = [k for k in st.get('knows', []) if not k.startswith(f'ep{num}:')]
    # Recompute world clock from remaining timeline
    if canon['timeline']:
        last = max(canon['timeline'], key=lambda t: t.get('ep', 0))
        canon['world_clock']['current_day'] = last.get('day', 0)
        canon['world_clock']['last_episode'] = last.get('ep', 0)
    else:
        canon['world_clock'] = {'current_day': 0, 'last_episode': 0}
    save_canon(sid, canon)


