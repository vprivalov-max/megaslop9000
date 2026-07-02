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
