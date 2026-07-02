"""Small shared helpers: transliteration, slugs, asset filenames."""
import re

_TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def slugify(title):
    """Convert series/character title to a safe folder name."""
    s = ''.join(_TRANSLIT.get(c.lower(), c) if c.lower() in _TRANSLIT else c for c in title)
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s_-]+', '_', s).strip('_')
    return s[:40] or 'series'

def asset_name(*parts):
    """Create UPPER_SNAKE_CASE asset filename stem from name parts.
    e.g. asset_name('Claire', 'Work Blazer') → 'CLAIRE_WORK_BLAZER'
         asset_name("Sophie's Apartment") → 'SOPHIES_APARTMENT'
    """
    combined = '_'.join(str(p) for p in parts)
    combined = re.sub(r"['\"]", '', combined)       # strip apostrophes/quotes
    combined = re.sub(r'[^\w]+', '_', combined)     # non-word chars → underscore
    return combined.upper().strip('_')

