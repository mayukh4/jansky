"""Jansky's tools, and the router that picks them.

A 1.7B model cannot be trusted to decide when to call a tool (it scored 17/24 and invents weather),
so the basics are routed by pattern first, like v1 did, and only what no pattern claims goes to the LLM.
Every tool returns (opener, text) or (opener, text, card): the opener is a cached clip that plays at
once while the answer is synthesised, and the card is what the face shows. No API keys: Open-Meteo, Google News RSS and Wikipedia are free.
Home Assistant (home.py) is on the local network, with a token from ~/jansky-v2/.env.
"""
import datetime
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import config, memory
from .home import QUERY, Home
from .todo import VERB_FORMS, TodoList

UA = {"User-Agent": "Jansky/2.0 (desk robot; https://youtube.com/@MayukhBuilds)"}


def fetch(url, timeout=5):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=5):
    return json.loads(fetch(url, timeout))


# ---------------------------------------------------------------- numbers and durations

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2, "few": 3,
}
UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}
DURATION = re.compile(
    r"(\d+(?:\.\d+)?|(?:(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|ninety|couple|few)"
    r"[\s-]?){1,3}?)\s*(?:of\s+)?(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b(\s+and\s+a\s+half)?")


def to_number(words):
    words = words.strip()
    try:
        return float(words)
    except ValueError:
        return float(sum(NUMBER_WORDS.get(w, 0) for w in re.split(r"[\s-]+", words)))


def parse_duration(text):
    t = text.lower().replace("half an hour", "30 minutes").replace("half a minute", "30 seconds")
    total = 0.0
    for m in DURATION.finditer(t):
        n = to_number(m.group(1))
        unit = UNITS[re.sub(r"s$", "", m.group(2))]
        total += (n + (0.5 if m.group(3) else 0)) * unit
    return int(total) if total > 0 else None


def say_duration(seconds):
    parts = []
    for name, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " and ".join(parts) or "no time"


# ---------------------------------------------------------------- time

def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def clock_card(now):
    return {"kind": "clock", "time": now.strftime("%-I:%M"), "day": f"{now.strftime('%A, %B')} {ordinal(now.day)}"}


def tell_time(_text):
    now = datetime.datetime.now()
    return ("Right,", f"It's {now.strftime('%-I:%M %p').replace('AM', 'a.m.').replace('PM', 'p.m.')}",
            clock_card(now))


def tell_date(_text):
    now = datetime.datetime.now()
    return "Right,", f"It's {now.strftime('%A')}, {now.strftime('%B')} {ordinal(now.day)}.", clock_card(now)


# ---------------------------------------------------------------- weather (Open-Meteo)

WMO = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
CITY = re.compile(r"\b(?:in|for|at)\s+([a-z][a-z .'-]+?)(?:\s+(?:today|tomorrow|tonight|right now|now|this week))?[?.!]*$")
NOT_CITIES = {"the morning", "the evening", "the afternoon", "the weekend", "general", "the area", "here"}


def find_place(text):
    m = CITY.search(text.lower())
    if not m or m.group(1).strip() in NOT_CITIES:
        return config.HOME_CITY, config.HOME_LAT, config.HOME_LON
    name = m.group(1).strip()
    q = urllib.parse.quote(name)
    hits = fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json")
    if not hits.get("results"):
        return None
    h = hits["results"][0]
    return h["name"], h["latitude"], h["longitude"]


def rain_words(pct):
    if pct is None or pct < 10:
        return "and no rain expected"
    return f"with a {pct} percent chance of rain"


def weather(text):
    place = find_place(text)
    if place is None:
        return "Hmm,", "I couldn't find that place on the map."
    name, lat, lon = place
    data = fetch_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&timezone=auto&forecast_days=2")
    d = data["daily"]
    if "tomorrow" in text.lower():
        sky = WMO.get(d["weather_code"][1], "mixed weather")
        hi, lo, rain = round(d["temperature_2m_max"][1]), round(d["temperature_2m_min"][1]), rain_words(d["precipitation_probability_max"][1])
        card = {"kind": "weather", "temp": hi, "sky": sky, "city": name, "detail": f"Tomorrow, low of {lo}, {rain.replace('and ', '')}."}
        return "Let me check.", f"Tomorrow in {name}: {sky}, a high of {hi} and a low of {lo} degrees, {rain}.", card
    c = data["current"]
    temp, feels = round(c["temperature_2m"]), round(c["apparent_temperature"])
    feel = f", feels like {feels}" if abs(feels - temp) >= 3 else ""
    sky, hi, rain = WMO.get(c["weather_code"], "mixed"), round(d["temperature_2m_max"][0]), rain_words(d["precipitation_probability_max"][0])
    card = {"kind": "weather", "temp": temp, "sky": sky, "city": name, "detail": f"High of {hi}, {rain.replace('and ', '')}."}
    return "Let me check.", f"It's {temp} degrees and {sky} in {name}{feel}. Today's high is {hi}, {rain}.", card


# ---------------------------------------------------------------- news (Google News RSS)

# Google News section feeds (proper curated headlines); anything else becomes a search.
SECTIONS = {"tech": "TECHNOLOGY", "technology": "TECHNOLOGY", "science": "SCIENCE", "business": "BUSINESS",
            "sports": "SPORTS", "sport": "SPORTS", "world": "WORLD", "canada": "NATION", "health": "HEALTH",
            "entertainment": "ENTERTAINMENT"}
SEARCH_TOPICS = {"space": "space", "ai": "artificial intelligence", "astronomy": "astronomy"}


def news(text):
    t = text.lower()
    m = re.search(r"news (?:about|on|from|regarding)\s+(.+?)[?.!]*$", t)
    section = next((k for k in SECTIONS if re.search(rf"\b{k}\b", t)), None)
    topic = m.group(1) if m else next((v for k, v in SEARCH_TOPICS.items() if re.search(rf"\b{k}\b", t)), None)
    region = "hl=en-CA&gl=CA&ceid=CA:en"
    if topic:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}+when:2d&{region}"
    elif section:
        url = f"https://news.google.com/rss/headlines/section/topic/{SECTIONS[section]}?{region}"
        topic = "technology" if section == "tech" else section
    else:
        url = f"https://news.google.com/rss?{region}"
    items = ET.fromstring(fetch(url)).findall("./channel/item")[:3]
    titles = [re.sub(r"\s+-\s+[^-]+$", "", i.findtext("title", "")).strip() for i in items]
    titles = [t for t in titles if t]
    if not titles:
        return "Hmm,", "I couldn't find any headlines just now."
    lead = f"Here's the latest on {topic}." if topic else "Here are the top headlines."
    return "Got it.", lead + " " + " ... ".join(f"{t}." for t in titles), {"kind": "news", "headlines": titles}


# ---------------------------------------------------------------- facts (Wikipedia)

# "what's a black hole", "who was Karl Jansky", and Whisper's "what a black hole" (dropped 's).
FACT = re.compile(r"^(?:so |and |okay |hey )?(?:what|who)(?:(?:'s|s| is| was| are| were)\s+(?:a |an |the )?|\s+(?:a|an)\s+)(.+?)[?.!]*$"
                  r"|^tell me (?:about|something about)\s+(?:a |an |the )?(.+?)[?.!]*$")
# Superlatives go to the LLM: Wikipedia's page for "biggest planet" is a list of exoplanets, read out in 6 s.
NOT_FACTS = re.compile(r"\b(you|your|my|me|i|weather|news|time|date|day|up|going on|plus|minus|times|divided)\b|\d"
                       r"|\b(biggest|largest|smallest|tallest|highest|longest|shortest|fastest|slowest|oldest|youngest|hottest"
                       r"|coldest|deepest|heaviest|brightest|closest|nearest|farthest|furthest|strongest|richest|greatest"
                       r"|most|least|best|worst)\b")


def wiki_topic(text):
    t = text.lower().strip()
    m = FACT.match(t)
    if not m:
        return None
    if re.match(r"^(?:so |and |okay |hey )?(?:what|who)\s+(?:a|an)\s", t) and not t.endswith("?"):
        return None  # "what a beautiful day!" is not a question
    topic = (m.group(1) or m.group(2) or "").strip()
    if not topic or len(topic.split()) > 5 or NOT_FACTS.search(topic):
        return None
    return topic


def wikipedia(topic):
    title = urllib.parse.quote(topic.replace(" ", "_"))
    try:
        page = fetch_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true")
    except Exception:
        page = None
    if not page or page.get("type") != "standard":
        q = urllib.parse.quote(topic)
        hits = fetch_json(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit=1&format=json")
        results = hits.get("query", {}).get("search", [])
        if not results:
            return None
        title = urllib.parse.quote(results[0]["title"].replace(" ", "_"))
        page = fetch_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true")
        if page.get("type") != "standard":
            return None
    extract = re.sub(r"\s*\([^()]*\)", "", page.get("extract", ""))
    sentences = re.split(r"(?<=[.!?])\s+", extract.strip())
    out = sentences[0]
    if len(sentences) > 1 and len((out + " " + sentences[1]).split()) <= 40:  # spoken answers stay short
        out += " " + sentences[1]
    return (page.get("title", topic), out) if len(out) > 20 else None


# ---------------------------------------------------------------- jokes (local, so they are actually funny)

JOKES = [
    "Why did the radio astronomer break up with the sun? Too much interference.",
    "I tried to tell a joke about the Milky Way. It was a bit of a spiral.",
    "What do you call a radio telescope that tells lies? A dish-honest instrument.",
    "Why don't black holes ever get invited to parties? They suck the life out of the room.",
    "How do you organise a space party? You planet.",
    "I'd tell you a UDP joke, but you might not get it.",
    "Why was the Raspberry Pi so calm? It had excellent cooling strategies. Well, it does now that I have a fan.",
    "What's a robot's favourite snack? Computer chips. Obviously.",
    "Why did the photon refuse to check a bag? It was travelling light.",
    "Karl Jansky heard the Milky Way hiss in 1932. Honestly, same. I hiss at Mondays.",
    "Why do astronomers make terrible comedians? Their jokes take light years to land.",
    "There are ten kinds of people: those who understand binary, and those who don't.",
    "Why did the neutron get a free drink? No charge.",
    "My code never has bugs. It just develops random features.",
    "What did the AI HAT say to the Raspberry Pi? I've got your back. And your top, technically.",
]
_last_jokes = []


SPACE_WORDS = re.compile(r"\b(space|star|planet|radio|telescope|astronom\w*|galaxy|milky|black hole|photon|sun)\b")


def joke(text):
    choices = [j for j in JOKES if j not in _last_jokes] or JOKES
    if SPACE_WORDS.search(text.lower()):
        choices = [j for j in choices if SPACE_WORDS.search(j.lower())] or choices
    j = random.choice(choices)
    _last_jokes.append(j)
    del _last_jokes[:-8]
    return "Ha!", j


# ---------------------------------------------------------------- the Pi itself

def system_status(_text):
    temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    mem = dict(line.split(":") for line in open("/proc/meminfo"))
    total = int(mem["MemTotal"].split()[0]) / 1e6
    avail = int(mem["MemAvailable"].split()[0]) / 1e6
    up = float(open("/proc/uptime").read().split()[0])
    uptime = say_duration(int(up) // 60 * 60)
    mood = "nice and comfy" if temp < 65 else "a bit toasty" if temp < 75 else "properly hot, I need that heatsink"
    card = {"kind": "system", "cpu": temp, "mood": mood.split(",")[0],
            "detail": f"{total - avail:.1f} of {total:.0f} GB memory in use. Awake for {uptime}."}
    return "Right,", (f"my CPU is at {temp:.0f} degrees, {mood}. I'm using {total - avail:.1f} of {total:.0f} "
                      f"gigabytes of memory, and I've been awake for {uptime}."), card


# ---------------------------------------------------------------- timers

REMINDER_VERBS = {"check", "call", "take", "go", "get", "turn", "start", "stop", "buy", "feed", "water", "send", "email", "text", "pick"}


class Timers:
    def __init__(self, announce, on_change=None):
        self.announce = announce    # called with text when a timer fires
        self.on_change = on_change  # called with the running timers, for the face
        self.active = {}            # id -> (label, end time, threading.Timer, total seconds)
        self.lock = threading.Lock()
        self.next_id = 0

    def handle(self, text):
        t = text.lower()
        if re.search(r"\b(cancel|stop|delete|clear|end|kill)\b", t):
            with self.lock:
                said = set(re.findall(r"[a-z]+", t)) - {"timer", "timers", "the", "my", "cancel", "stop", "delete", "clear"}
                named = [tid for tid, e in self.active.items() if said & set(e[0].split()) - {"timer", "reminder"}]
                hit = named or list(self.active)
                for tid in hit:
                    self.active.pop(tid)[2].cancel()
            self._changed()
            if not hit:
                return "Okay,", "There aren't any timers running."
            return "Okay,", "Stopped." if len(hit) == 1 else f"Stopped all {len(hit)} timers."
        if re.search(r"\b(how (much|long)|left|remaining|status)\b", t) and not parse_duration(t):
            with self.lock:
                left = [(label, end - time.time()) for label, end, _, _ in self.active.values()]
            if not left:
                return "Hmm,", "No timers running."
            return "Right,", " ".join(
                f"{say_duration(int(s))} until I remind you to {label}." if label.split()[0] in REMINDER_VERBS
                else f"{say_duration(int(s))} left on the {label} timer." for label, s in left)
        seconds = parse_duration(t)
        if not seconds:
            return "Hmm,", "How long should I set it for?"
        label = self._label(t)
        reminder = "remind" in t
        with self.lock:
            tid = self.next_id
            self.next_id += 1
            th = threading.Timer(seconds, self._fire, args=(tid,))
            name = label or ("reminder" if reminder else "timer")
            self.active[tid] = (name, time.time() + seconds, th, seconds)
            th.daemon = True
            th.start()
        self._changed()
        card = {"kind": "timer", "id": tid, "label": name, "end": time.time() + seconds, "total": seconds}
        if reminder and label:
            return "Got it.", f"I'll remind you to {label} in {say_duration(seconds)}.", card
        name = f"{label} timer" if label else "timer"
        return "Sure thing.", f"{say_duration(seconds).capitalize()} {name}, starting now.", card

    def items(self):
        with self.lock:
            return [{"id": tid, "label": label, "end": end, "total": total}
                    for tid, (label, end, _, total) in self.active.items()]

    def cancel(self, tid):
        """A timer dial was tapped on the screen."""
        with self.lock:
            entry = self.active.pop(tid, None)
            if entry:
                entry[2].cancel()
        self._changed()
        return entry is not None

    def _changed(self):
        if self.on_change:
            self.on_change(self.items())

    @staticmethod
    def _label(t):
        t = re.sub(r"\s+", " ", DURATION.sub(" ", t))
        cands = re.findall(r"\b(?:to|for)\s+(?:the\s+|my\s+)?([a-z][a-z' ]{2,40}?)\s*(?:$|[.?!,]|\bin\b)", t)
        cands = [re.sub(r"^(?:(?:for|to|the|my|a)\s+)+", "", c.strip()) for c in cands]
        cands = [c for c in cands if c and c not in ("me", "set", "timer", "a timer")]
        return cands[-1] if cands else ""

    def _fire(self, tid):
        with self.lock:
            entry = self.active.pop(tid, None)
        self._changed()
        if not entry:
            return
        label = entry[0]
        if label == "timer":
            text = "Time's up! Your timer is done."
        elif label == "reminder":
            text = "Here's your reminder."
        elif label.split()[0] in REMINDER_VERBS:  # "remind me to check the oven"
            text = f"Reminder: {label}."
        else:                                     # "a timer for the pasta"
            text = f"Time's up! Your {label} timer is done."
        self.announce(text)


# ---------------------------------------------------------------- reminders and the list (todo.py)

HOURS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)}
CLOCK = re.compile(r"\b(?:at|by|around)\s+(\d{1,2}|" + "|".join(HOURS) + r")(?:[:.](\d{2})|\s+(thirty|fifteen|forty[- ]five|o'?clock))?"
                   r"\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?|in the morning|in the afternoon|in the evening|tonight)?"
                   r"|\b(?:at\s+)?(noon|midday|midnight)\b")
DAYS = re.compile(r"\b(?:(?:on|by|this|next)\s+)?(today|tonight|tomorrow|this evening|this afternoon|this morning|this weekend|"
                  r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
ADD = re.compile(r"^(?:can you |could you |please |hey )?remind me\s*(to |that i (?:need|have|want) to |that |about |of |for )?(.*)$"
                 r"|^(?:please |can you )?(?:add|put) (.+?) (?:to|on|onto) (?:my |the )?(?:to-?do |todo |shopping |reminders? )?list$")
LIST = re.compile(r"\b(what'?s|what is|read|show|open|check)\b.*\b(?:my|the)\s+(?:to-?do\s+|todo\s+|shopping\s+)?(list|reminders|to-?dos)\b"
                  r"|\b(?:do i have any|any) (reminders|to-?dos)\b|\bwhat do i (?:need|have) to do\b")
DONE = re.compile(r"^(?:please\s+)?(?:mark|tick|cross)\s+(?:off\s+)?(.+?)(?:\s+(?:as\s+)?(?:done|off|complete|finished))?(?:\s+(?:on|from)\s+(?:my|the)\s+list)?$"
                  r"|^(?:please\s+)?(?:remove|delete|take)\s+(.+?)\s+(?:off|from)\s+(?:my |the )?(?:to-?do |shopping )?list$"
                  r"|^(?:i(?:'ve| have)?\s+)?(?:already\s+|just\s+)?((?:" + "|".join(k for k in VERB_FORMS if not k.endswith("ing")) + r")\s+.+)$")
CLEAR = re.compile(r"^(?:please\s+)?(?:clear|empty|wipe)\s+(?:out\s+)?(?:my |the )?(?:to-?do |todo |shopping )?(?:list|reminders)$")
CANCEL = re.compile(r"^(?:please\s+)?(?:cancel|delete|remove|stop|drop|forget|scrap|undo)\s+(?:(?:the|my|that|this|last|a|all|all of)\s+)*"
                    r"(?:(?:timed\s+)?reminders?)\b\s*(?:to |for |about |of |that )?(.*)$")
SET_REMINDER = re.compile(r"^(?:can you |could you |please )?(?:set|add|make|create) (?:a |me a |up a |another )?reminder\s*")
HIDE = re.compile(r"^(?:please\s+)?(?:hide|close)\s+(?:my |the )?(?:to-?do |shopping )?(?:list|reminders)$")


def say_clock(dt):
    return dt.strftime("%-I:%M %p").replace(":00", "").replace("AM", "a.m.").replace("PM", "p.m.")


def parse_when(t):
    """Pull the time out of a reminder. Returns (due epoch or None, label for the list, spoken suffix, the rest)."""
    now = datetime.datetime.now()
    due, label, spoken = None, "", ""
    d = DAYS.search(t)
    day_word = d.group(1) if d else ""
    if d:
        t = t[:d.start()] + t[d.end():]
    m = CLOCK.search(t)
    if m:
        if m.group(5):
            h, mi = {"noon": 12, "midday": 12, "midnight": 0}[m.group(5)], 0
        else:
            h = int(m.group(1)) if m.group(1).isdigit() else HOURS[m.group(1)]
            mi = int(m.group(2)) if m.group(2) else {"thirty": 30, "fifteen": 15}.get(m.group(3) or "", 45 if "forty" in (m.group(3) or "") else 0)
            ap = re.sub(r"[.\s]", "", m.group(4) or "")
            if ap.startswith("p") or ap in ("intheafternoon", "intheevening", "tonight") or day_word in ("tonight", "this evening"):
                h = h % 12 + 12
            elif ap.startswith("a") or ap == "inthemorning":
                h = h % 12
            elif h <= 6 and day_word in ("", "today"):  # "at 3" means the afternoon, never 3 a.m.
                h += 12
            elif h < 12 and day_word in ("", "today"):  # "at 9": the next 9 o'clock, a.m. or p.m.
                soon = [now.replace(hour=hh, minute=mi, second=0, microsecond=0) + datetime.timedelta(days=d)
                        for d in (0, 1) for hh in (h, h + 12)]
                h = min(c for c in soon if c > now).hour
            elif h < 8:  # "tomorrow at 5" is the afternoon, "tomorrow at 9" the morning
                h += 12
        target = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if day_word == "tomorrow":
            target += datetime.timedelta(days=1)
        elif target <= now:
            target += datetime.timedelta(days=1)
        due = target.timestamp()
        tomorrow = target.date() != now.date()
        label = ("Tomorrow " if tomorrow else "") + target.strftime("%-I:%M %p")
        spoken = (" tomorrow" if tomorrow else "") + f" at {say_clock(target)}"
        t = t[:m.start()] + t[m.end():]
    else:
        seconds = parse_duration(t)
        if seconds:
            target = now + datetime.timedelta(seconds=seconds)
            due, label, spoken = target.timestamp(), target.strftime("%-I:%M %p"), f" in {say_duration(seconds)}"
            t = re.sub(r"\b(?:in|after|for)\s+(?=\s*$|\s*,|\s+to\b)", "", DURATION.sub(" ", t))
        elif day_word:
            label = day_word[0].upper() + day_word[1:]
            spoken = f" {day_word}" if day_word in ("today", "tonight", "tomorrow") or day_word.startswith("this ") else f" on {label}"
    return due, label, spoken, re.sub(r"\s+", " ", t).strip(" ,.")


def say_when(label):
    """The list's "Tomorrow 9:00 AM" / "Friday" / "Tonight", as it is said out loud."""
    m = re.match(r"^(Tomorrow )?(\d{1,2}:\d{2} [AP]M)$", label)
    if m:
        clock = say_clock(datetime.datetime.strptime(m.group(2), "%I:%M %p")).rstrip(".")
        return ("tomorrow " if m.group(1) else "") + "at " + clock
    low = label.lower()
    return low if low in ("today", "tonight", "tomorrow") or low.startswith("this ") else f"on {label}"


class Reminders:
    def __init__(self, announce, on_change):
        self.list = TodoList(announce, on_change)

    @staticmethod
    def claims(t):
        t = SET_REMINDER.sub("remind me ", t)
        return bool(ADD.match(t) or LIST.search(t) or CLEAR.match(t) or HIDE.match(t) or DONE.match(t) or CANCEL.match(t))

    def handle(self, text):
        t = text.lower().strip().rstrip(".!?")
        t = re.sub(r"\b(please|thanks|thank you|okay)\b[,]?", "", t).strip(" ,")
        t = SET_REMINDER.sub("remind me ", t)  # "set a reminder after 5 minutes to drink water"
        m = CANCEL.match(t)
        if m:
            return self._cancel(t, m.group(1).strip())
        if CLEAR.match(t):
            n = self.list.clear()
            return "Okay,", "Your list is empty now." if n else "Your list was already empty."
        if HIDE.match(t):
            self.list.hide()
            return "Okay.", "Tucked away."
        if LIST.search(t) and not ADD.match(t):
            items = self.list.snapshot()
            self.list.show()
            if not items:
                return "Ooh,", "Your list is empty. Nothing to do!"
            spoken = [it["text"][0].lower() + it["text"][1:] + (f", {say_when(it['when'])}" if it["when"] else "") for it in items[:5]]
            n = len(items)
            lead = f"You've got {n} thing{'s' if n != 1 else ''} on your list."
            return "Right,", lead + " " + " ... ".join(s[0].upper() + s[1:] + "." for s in spoken)
        m = ADD.match(t)
        if m:
            prep, body = (m.group(1) or "to ").strip(), m.group(2) if m.group(2) is not None else m.group(3)
            due, label, spoken, body = parse_when(body)
            body = re.sub(r"^(?:to|that i (?:need|have) to|that|about|of|for)\s+", "", body).strip()
            words = body.split()
            if not words:
                return "Hmm,", "What should I remind you about?"
            if words[0].endswith("ing") and words[0] in VERB_FORMS:  # "remind me for buying a pen"
                words[0], prep = VERB_FORMS[words[0]], "to"
            body = " ".join(words)
            at = text.lower().find(body)  # keep the transcript's capitals: "email Dom", not "email dom"
            if at >= 0:
                body = text[at:at + len(body)]
            self.list.add(body[0].upper() + body[1:], label, due)
            if m.group(3) is not None:
                return "Got it.", f"Added {body} to your list."
            prep = "about" if prep in ("about", "of") else "to"
            return "Got it.", f"I'll remind you {prep} {body}{spoken}".rstrip(".") + "."
        m = DONE.match(t)
        if m:
            what = next(g for g in m.groups() if g)
            it = self.list.find(what)
            if not it:
                return None  # "I bought a new mic" is just chat when nothing on the list matches
            self.list.done(it["id"])
            left = len(self.list.items)
            tail = f" {left} left." if left else " That's everything!"
            return "Brilliant!", f"Ticked off: {it['text'][0].lower() + it['text'][1:]}.{tail}"
        return None


    def _cancel(self, t, what):
        """"Cancel the reminder" (the newest timed one), "...to drink water" (that one), "all reminders"."""
        items = self.list.snapshot()
        if not items:
            return "Hmm,", "There aren't any reminders to cancel."
        if re.search(r"\ball\b", t):
            timed = [it for it in items if it["due"]] or items
            for it in timed:
                self.list.done(it["id"])
            return "Okay,", f"Cancelled {len(timed)} reminder{'s' if len(timed) != 1 else ''}."
        it = self.list.find(what) if what else None
        if not it:
            pending = [x for x in items if x["due"] and not x["fired"]]
            it = (pending or items)[0]  # newest first
        self.list.done(it["id"])
        return "Okay,", f"Cancelled the reminder to {it['text'][0].lower() + it['text'][1:]}."


# ---------------------------------------------------------------- the router

class Router:
    """Pattern routing for the basics. Returns (opener, text, card or None), or None to let the LLM answer."""

    def __init__(self, announce, on_timers=None, on_todo=None):
        self.timers = Timers(announce, on_timers)
        self.reminders = Reminders(announce, on_todo)
        self.home = Home()
        self.last = None
        self.memory_changed = False  # the persona needs re-priming so the LLM knows

    def _routine(self, t):
        """Good night: everything off, and a sleepy face. Good morning: the weather and the list."""
        if re.match(r"^(?:good ?night|night night|nighty night)\b", t):
            self.last = "goodnight"  # main.py puts the face to sleep
            out = self.home.everything_off() if config.HA_TOKEN else None
            if out:
                return "Okay,", f"Good night, {config.USER}. {out[1]} Sleep well.", out[2]
            return "Okay,", f"Good night, {config.USER}. Sleep well.", None
        if re.match(r"^(?:please |can you |could you )?(?:introduce yourself|who are you)\b", t):
            self.last = "intro"  # the video's cold open: Jansky's own voice and face
            return ("Hello!", "I'm Jansky. I live on this desk, I run the lights, I remember what you tell me, "
                              "and your voice never leaves this room.", None)
        if re.match(r"^good morning\b", t):
            self.last = "goodmorning"
            _, sky, card = weather("")
            items = self.reminders.list.snapshot()
            todo = (f" You've got {len(items)} thing{'s' if len(items) != 1 else ''} on your list, starting with "
                    f"{items[0]['text'][0].lower() + items[0]['text'][1:]}." if items else "")
            if items:
                self.reminders.list.show()
            return "Hello!", f"Good morning, {config.USER}. {sky}{todo}", card
        return None

    def _memory(self, t, text):
        """Remember / recall / forget. Returns a reply, or None if this is not a memory request."""
        if re.search(r"\bwhat do you (know|remember) about me\b|\bwhat have i told you\b|\bwhat'?s in your memory\b", t):
            facts = memory.facts()[-4:]
            if not facts:
                return "Hmm,", "I don't know much about you yet. Tell me something and say remember.", None
            said = [memory.to_you(f) for f in facts]
            return ("Ooh,", "Here's what I know. " + " ... ".join(s[0].upper() + s[1:] + "." for s in said),
                    {"kind": "memory", "title": "What I know about you", "text": " ".join(s[0].upper() + s[1:] + "." for s in said)})
        if re.match(r"^(?:please |okay |so )?forget\b", t):
            n = memory.forget(text)
            self.memory_changed = self.memory_changed or n > 0
            return ("Okay,", "Forgotten." if n else "I couldn't find that in my memory.", None)
        question = t.endswith("?") or re.match(r"^(do you|can you remember|did|what|who|when|where|how)\b", t)
        if re.search(r"\b(remember|don'?t forget|do not forget|keep in mind|for the record)\b", t) and not question:
            fact = memory.add(text)
            if not fact:
                return "Hmm,", "What should I remember?", None
            self.memory_changed = True
            you = memory.to_you(fact)
            return ("Got it.", f"I'll remember that {you}.",
                    {"kind": "memory", "title": "I'll remember", "text": you[0].upper() + you[1:] + "."})
        return None

    def act(self, command):
        """Run a command the LLM wrote ("[on: fan]", "[remind: the dentist | Friday]") through the same tools the
        patterns use. Returns (opener, text, card), or None if the command is not one Jansky knows."""
        m = re.match(r"^\s*(on|off|remind|timer|night)\s*:?\s*(.*?)\s*$", command.lower())
        if not m:
            return None
        verb, arg = m.groups()
        if verb in ("on", "off") and config.HA_TOKEN:
            out = self.home.handle(f"turn {verb} the {arg}")
            self.last = "home"
        elif verb == "remind" and arg:
            what, _, when = arg.partition("|")
            when = re.sub(r"^(?:when|at time|time)\s*:?\s*", "", when.strip())  # the model copies the template word
            out = self.reminders.handle(f"remind me to {what.strip()} {when}".strip())
            self.last = "reminders"
        elif verb == "timer" and parse_duration(arg if re.search(r"[a-z]", arg) else f"{arg} minutes"):
            out = self.timers.handle(f"set a timer for {arg if re.search(r'[a-z]', arg) else arg + ' minutes'}")
            self.last = "timer"
        elif verb == "night":
            out = self._routine("good night")
        else:
            return None
        return out if out and len(out) == 3 else (*out, None) if out else None

    def route(self, text, spotted=None):
        """`spotted` is a home command the keyword spotter heard in the same audio, e.g. ("fan", True)."""
        t = text.lower().strip()
        bare = t.rstrip(".!?")
        # A bare "stop" / "cancel" stops running timers (talking over a reply already stops the reply).
        if re.match(r"^(?:please\s+)?(?:stop|cancel|stop it|cancel it|cancel that|stop that|stop the alarm)$", bare.strip(" ,")):
            if self.timers.items():
                self.last = "timer"
                return (*self.timers.handle("cancel"), None)
            return "Okay.", "", None
        # The list and the house first: "remind me to turn off the fan" is a reminder, not the fan.
        claimed = [("routine", self._routine)]
        is_reminder = Reminders.claims(re.sub(r"\b(please|thanks|thank you|okay)\b[,]?", "", bare).strip(" ,"))
        if is_reminder:
            claimed.append(("reminders", lambda _t: self.reminders.handle(text)))
        if spotted and not is_reminder and not QUERY.match(bare):
            claimed.append(("home", lambda _t: self.home.spotted(*spotted)))
        if Home.claims(bare):
            claimed.append(("home", lambda _t: self.home.handle(text)))
        for name, fn in claimed:
            try:
                out = fn(t)
            except Exception as e:
                print(json.dumps({"event": "tool_error", "tool": name, "error": str(e)}), flush=True)
                return "Oh dear,", f"Something went wrong with the {'home' if name == 'home' else name}.", None
            if out:
                if name != "routine":
                    self.last = name
                return out if len(out) == 3 else (*out, None)
        follow = self.last == "weather" and re.match(r"^(and|what about|how about)\b", t)
        rules = [
            ("timer", r"\b(timers?|remind me|alarms?|countdown|stopwatch)\b|\b(set|start)\b.*\b(minutes?|seconds?|hours?)\b", self.timers.handle),
            ("time", r"\bwhat time\b|\bthe time\b|\btime is it\b", tell_time),
            ("date", r"\bwhat(?:'s| is)? (?:the )?date\b|\bwhat day\b|\btoday's date\b", tell_date),
            ("system", r"\b(cpu|your temperature|how hot are you|your memory|ram|uptime|system status|overheating)\b", system_status),
            ("weather", r"\b(weather|forecast|temperature|raining|rain|snowing|snow|sunny|umbrella|jacket)\b", weather),
            ("news", r"\b(news|headlines?)\b", news),
            ("joke", r"\bjokes?\b|\bmake me laugh\b|\bsomething funny\b", joke),
        ]
        # Timers first ("remind me to ..."), then memory ("remember that ..."), then everything else.
        if re.search(rules[0][1], t):
            rules = rules[:1]
        else:
            mem = self._memory(t, text)
            if mem:
                self.last = "memory"
                return mem
        for name, pattern, fn in rules:
            if re.search(pattern, t) or (follow and name == "weather"):
                self.last = name
                try:
                    out = fn(text)
                    return out if len(out) == 3 else (*out, None)
                except Exception as e:  # network down, API changed: say so instead of going quiet
                    print(json.dumps({"event": "tool_error", "tool": name, "error": str(e)}), flush=True)
                    return "Oh dear,", f"I couldn't reach the {name} service just now.", None
        topic = wiki_topic(t)
        if topic:
            try:
                answer = wikipedia(topic)
            except Exception:
                answer = None
            if answer:
                self.last = "facts"
                title, text = answer
                return "Ooh,", text, {"kind": "fact", "title": title, "text": text}
        self.last = None
        return None
