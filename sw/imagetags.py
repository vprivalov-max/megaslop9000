"""@ImageN tag remapping after server-side ref filtering."""
import re

def _remap_image_tags(prompt_text: str, old_to_new: dict) -> str:
    """After server-side ref filtering (close-up / de-dup / unresolved drops),
    the composer's prompt still contains @ImageN references for ALL refs it
    originally chose. If we filter refs[] but leave the prompt alone, Seedance
    gets a prompt mentioning @ImageN with no actual reference image at slot N
    → it hallucinates a random face for that 'character'. Real bug seen in
    production: chunk had refs=[Adrian, Lobby] but prompt said @Image2=Vivian,
    @Image3=Sophie, @Image4=Clara → those positions had no images and Seedance
    drew arbitrary people.

    `old_to_new` maps composer's original 1-based @ImageN index → new index
    in the filtered refs[] (None = dropped). This function:
      • Renumbers kept @ImageN to their new positions.
      • Strips entire `@ImageN=Name (clothing)` clauses for dropped N (BINDING).
      • Strips bare `@ImageN` tokens for dropped N (DIALOGUE / ACTION).
      • Tries to clean up dangling commas / empty parens left behind.
    """
    if not prompt_text or not old_to_new:
        return prompt_text or ''
    has_drops = any(v is None for v in old_to_new.values())
    needs_renumber = any(k != v for k, v in old_to_new.items() if v is not None)
    if not has_drops and not needs_renumber:
        return prompt_text

    # We use a placeholder string \x01IMG\x01<n> for renumbered tokens so
    # Pass 2's `@Image\d+` regex doesn't accidentally rewrite them again.
    KEPT_PRE = '\x01IMG\x01'
    DROP_MARK = '\x02'

    # Pass 1: BINDING-style clauses '@ImageN=Name (description)' or '@ImageN=Name'
    # Stops at next comma / period / newline / @Image so adjacent BINDING entries
    # don't bleed together.
    binding_re = re.compile(
        r'@Image(\d+)\s*[=—\-]\s*[^,.@\n]+?(?:\s*\([^)]*\))?(?=\s*(?:,|\.|\n|@Image|$))'
    )
    def _replace_binding(m):
        oldN = int(m.group(1))
        newN = old_to_new.get(oldN)
        if newN is None:
            return DROP_MARK
        # Renumber the @ImageN at the start; placeholder so Pass 2 ignores
        return re.sub(r'^@Image\d+', f'{KEPT_PRE}{newN}', m.group(0), count=1)
    prompt_text = binding_re.sub(_replace_binding, prompt_text)

    # Pass 2: bare @ImageN remaining (DIALOGUE shot-bits, parens). Placeholder
    # tokens from pass 1 are \x01IMG\x01N — don't match @Image\d+ pattern.
    def _replace_bare(m):
        oldN = int(m.group(1))
        newN = old_to_new.get(oldN)
        return f'{KEPT_PRE}{newN}' if newN is not None else DROP_MARK
    prompt_text = re.sub(r'@Image(\d+)', _replace_bare, prompt_text)

    # Convert placeholders back to @Image
    prompt_text = prompt_text.replace(KEPT_PRE, '@Image')

    # Strip drop-marks plus any trailing punctuation/glue token that would
    # leave dangling artefacts.
    prompt_text = re.sub(rf'{DROP_MARK}\s*[,.]?\s*', '', prompt_text)

    # Cleanup punctuation / spacing artefacts left by deletions
    prompt_text = re.sub(r'\(\s*\)', '', prompt_text)             # empty parens
    prompt_text = re.sub(r',\s*,+', ',', prompt_text)             # double commas
    prompt_text = re.sub(r',\s*\.', '.', prompt_text)             # ", ." → "."
    prompt_text = re.sub(r'\(\s*,', '(', prompt_text)             # "(," → "("
    prompt_text = re.sub(r',\s*\)', ')', prompt_text)             # ",)" → ")"
    prompt_text = re.sub(r' {2,}', ' ', prompt_text)              # multiple spaces
    prompt_text = re.sub(r'\s*\n\s*\n\s*\n+', '\n\n', prompt_text)
    return prompt_text.strip()


def _build_image_tag_remap(original_refs: list, final_refs: list) -> dict:
    """Build {original_1based_idx: final_1based_idx_or_None} from composer's
    original ordered refs vs the filtered ordered refs."""
    orig_pos = {}      # (kind, id) → original 1-based pos (first occurrence)
    for i, r in enumerate(original_refs or []):
        key = (r.get('kind'), r.get('id'))
        if key not in orig_pos:
            orig_pos[key] = i + 1
    final_pos = {}
    for i, r in enumerate(final_refs or []):
        key = (r.get('kind'), r.get('id'))
        if key not in final_pos:
            final_pos[key] = i + 1
    out = {}
    for key, oldN in orig_pos.items():
        out[oldN] = final_pos.get(key)
    return out

