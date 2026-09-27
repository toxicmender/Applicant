"""Whether a posting's location is inside the one you asked for.

Boards answer a country search with bare city names - "Bengaluru", not
"Bengaluru, India" - which is fine while the board is doing the filtering and
useless the moment we have to check it ourselves, as `applicant apply` does
against a stored file. A plain text match of "India" against "Bengaluru,
Karnataka" is false, so the filter that should have narrowed a worklist emptied
it instead.

So: a table of what is inside India, and the country names that say a posting is
somewhere else. India because that is where this tool is pointed - Naukri,
AmbitionBox and a checked-in INR conversion factor - and because a table nobody
maintains is worse than an honest "cannot tell".

Which is the third answer here. `within()` returns None when the question cannot
be settled from what we know, and the caller keeps the posting and flags it,
rather than dropping a result that may well be correct.
"""

from __future__ import annotations

import re

# States, union territories and the cities that actually appear in postings.
# Both spellings wherever a city was renamed, since boards use either.
INDIA = {
    # states and union territories
    'andhra pradesh',
    'arunachal pradesh',
    'assam',
    'bihar',
    'chhattisgarh',
    'goa',
    'gujarat',
    'haryana',
    'himachal pradesh',
    'jharkhand',
    'karnataka',
    'kerala',
    'madhya pradesh',
    'maharashtra',
    'manipur',
    'meghalaya',
    'mizoram',
    'nagaland',
    'odisha',
    'orissa',
    'punjab',
    'rajasthan',
    'sikkim',
    'tamil nadu',
    'telangana',
    'tripura',
    'uttar pradesh',
    'uttarakhand',
    'west bengal',
    'delhi',
    'jammu',
    'kashmir',
    'ladakh',
    'puducherry',
    'pondicherry',
    'chandigarh',
    'andaman',
    'nicobar',
    'lakshadweep',
    'daman',
    'diu',
    # cities
    'bengaluru',
    'bangalore',
    'hyderabad',
    'secunderabad',
    'pune',
    'pimpri',
    'chinchwad',
    'mumbai',
    'bombay',
    'navi mumbai',
    'thane',
    'chennai',
    'madras',
    'kolkata',
    'calcutta',
    'new delhi',
    'noida',
    'greater noida',
    'gurugram',
    'gurgaon',
    'ghaziabad',
    'faridabad',
    'ahmedabad',
    'gandhinagar',
    'surat',
    'vadodara',
    'rajkot',
    'jaipur',
    'jodhpur',
    'udaipur',
    'kota',
    'indore',
    'bhopal',
    'nagpur',
    'nashik',
    'aurangabad',
    'kolhapur',
    'solapur',
    'coimbatore',
    'madurai',
    'tiruchirappalli',
    'trichy',
    'salem',
    'tirupur',
    'kochi',
    'cochin',
    'ernakulam',
    'thiruvananthapuram',
    'trivandrum',
    'kozhikode',
    'calicut',
    'thrissur',
    'mysuru',
    'mysore',
    'mangaluru',
    'mangalore',
    'hubli',
    'dharwad',
    'belagavi',
    'belgaum',
    'lucknow',
    'kanpur',
    'varanasi',
    'agra',
    'meerut',
    'prayagraj',
    'allahabad',
    'gorakhpur',
    'visakhapatnam',
    'vizag',
    'vijayawada',
    'guntur',
    'nellore',
    'tirupati',
    'warangal',
    'bhubaneswar',
    'cuttack',
    'patna',
    'ranchi',
    'jamshedpur',
    'raipur',
    'guwahati',
    'dehradun',
    'shimla',
    'srinagar',
    'amritsar',
    'ludhiana',
    'jalandhar',
    'mohali',
    'panchkula',
    'karnal',
    'panipat',
    'rohtak',
    'hisar',
    'siliguri',
    'howrah',
    'durgapur',
    'bhavnagar',
    'jamnagar',
    'bareilly',
    'aligarh',
    'moradabad',
    'jhansi',
    'sangli',
    'satara',
}

WITHIN = {'india': INDIA}

# Which country each known place belongs to.
COUNTRY_OF_PLACE = {place: country for country, places in WITHIN.items() for place in places}

# Enough country names to recognise that a posting is somewhere *else*. Ambiguous
# ones are left out on purpose: Georgia is a country and a US state, and Jersey
# is an island and a city, so neither can settle anything on its own.
COUNTRIES = {
    'india',
    'united states',
    'usa',
    'us',
    'america',
    'canada',
    'mexico',
    'brazil',
    'argentina',
    'chile',
    'colombia',
    'peru',
    'united kingdom',
    'uk',
    'england',
    'scotland',
    'wales',
    'northern ireland',
    'ireland',
    'france',
    'germany',
    'spain',
    'portugal',
    'italy',
    'netherlands',
    'belgium',
    'luxembourg',
    'switzerland',
    'austria',
    'denmark',
    'norway',
    'sweden',
    'finland',
    'iceland',
    'poland',
    'czechia',
    'czech republic',
    'slovakia',
    'hungary',
    'romania',
    'bulgaria',
    'greece',
    'croatia',
    'serbia',
    'estonia',
    'latvia',
    'lithuania',
    'ukraine',
    'russia',
    'turkey',
    'israel',
    'united arab emirates',
    'uae',
    'saudi arabia',
    'qatar',
    'kuwait',
    'bahrain',
    'oman',
    'egypt',
    'morocco',
    'south africa',
    'kenya',
    'nigeria',
    'ghana',
    'ethiopia',
    'china',
    'hong kong',
    'taiwan',
    'japan',
    'south korea',
    'north korea',
    'singapore',
    'malaysia',
    'indonesia',
    'thailand',
    'vietnam',
    'philippines',
    'cambodia',
    'australia',
    'new zealand',
    'sri lanka',
    'bangladesh',
    'pakistan',
    'nepal',
    'bhutan',
    'maldives',
    'myanmar',
    'afghanistan',
    'iran',
    'iraq',
    'jordan',
    'lebanon',
}


def _pattern(names) -> re.Pattern[str]:
    """One alternation, longest first so 'new delhi' wins over 'delhi'."""
    ordered = sorted(names, key=len, reverse=True)
    return re.compile(r'\b(?:{})\b'.format('|'.join(re.escape(name) for name in ordered)))


# word boundaries throughout, so 'India' never matches 'Indiana'
PLACES = _pattern(COUNTRY_OF_PLACE)
NATIONS = _pattern(COUNTRIES)


def names(target: str, text: str) -> bool:
    """Does `text` use every word of `target`, in any order, as a whole word?

    Whole words matter here in a way they do not for a job title: "India" is
    inside "Indianapolis, Indiana", and a location filter that answers an Indian
    search with Indiana is worse than one that answers nothing.
    """
    return all(
        re.search(r'\b{}\b'.format(re.escape(word)), text) is not None for word in target.split()
    )


def countries_in(location: str | None) -> set[str]:
    """Every country `location` names, directly or through a place we know."""
    text = (location or '').lower()
    found = set(NATIONS.findall(text))
    found.update(COUNTRY_OF_PLACE[place] for place in PLACES.findall(text))
    return found


def country_for(location: str | None) -> str | None:
    """The country a location is in, when exactly one is implied.

    'Bengaluru' -> 'india'. Used to reach the right country site on a board that
    picks its host from the search string, so a bare city name does not quietly
    return jobs from the other side of the world.
    """
    found = countries_in(location)
    return found.pop() if len(found) == 1 else None


def within(wanted: str, actual: str | None) -> bool | None:
    """Is `actual` inside `wanted`? None when what we know cannot settle it.

    Text matching first, so a city or a state filter behaves exactly as it always
    has. Only a country filter consults the table, because only a country filter
    is routinely asked about a location that never spells it out.
    """
    target = (wanted or '').strip().lower()
    text = (actual or '').strip().lower()
    if not target:
        return True
    if not text:
        return False  # a posting with no location cannot be shown to be in one
    if names(target, text):
        return True

    if target not in COUNTRIES:
        # a city or state filter, already answered by the text match above
        return False

    implied = countries_in(text)
    if target in implied:
        return True
    if implied:
        return False  # somewhere we can name, and it is not where you asked
    return None  # a place we have never heard of: say so rather than guess
