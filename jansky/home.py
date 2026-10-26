"""Home Assistant: switch the lights and the fan by voice.

Jansky reads the device list from Home Assistant's REST API (a long-lived token, config.HA_TOKEN), caches
it, and matches what was said against the names given to the devices in HA. Entities that share a name
are one appliance and switch together: "Desk LED" (the LED strip) and "Desk LED Socket 1" (the plug that
powers it) are both "the desk light". Matching is by pattern, not by the LLM, like the other basics.
"""
import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import config

SYNONYMS = {"led": "light", "leds": "light", "lights": "light", "lamp": "light", "lamps": "light", "bulb": "light",
            "strip": "light", "fans": "fan"}
FILLER = {"the", "my", "a", "an", "and", "of", "in", "please", "socket", "plug", "switch"}
ACTION = re.compile(r"\b(turn|switch|put|shut|flip|power)\b.*?\b(on|off)\b|\b(toggle)\b|^(?:lights?|fan|everything)\s+(on|off)\b")
QUERY = re.compile(r"^(?:is|are)\s+(?:the\s+|my\s+)?(.+?)\s+(on|off|running)\b")
ALL = re.compile(r"\b(everything|all the|all of the|all)\b")
REFRESH_S = 300


def sound(text):
    """A rough consonant skeleton: "desk light" -> "dsklt", "disclate" -> "dsklt"."""
    t = re.sub(r"[^a-z]", "", text.lower())
    for x, y in (("ght", "t"), ("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("x", "ks")):
        t = t.replace(x, y)
    return t[:1] + re.sub(r"[aeiouyhw]", "", t[1:]) if t else ""


def words(name):
    return [SYNONYMS.get(w, w) for w in re.findall(r"[a-z]+", name.lower()) if w not in FILLER]


class Home:
    def __init__(self):
        self.appliances = {}  # "desk light" -> [{"id", "domain", "state"}]
        self.fetched = 0.0
        self.lock = threading.Lock()

    # ---- REST ----

    def _request(self, path, body=None, timeout=4):
        req = urllib.request.Request(config.HA_URL + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {config.HA_TOKEN}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def refresh(self):
        found = {}
        for e in self._request("/api/states"):
            domain = e["entity_id"].split(".")[0]
            name = e["attributes"].get("friendly_name") or e["entity_id"].split(".")[1].replace("_", " ")
            if domain not in config.HA_DOMAINS or any(s in name.lower() for s in config.HA_SKIP):
                continue
            key = " ".join(words(name))
            if key:
                found.setdefault(key, []).append({"id": e["entity_id"], "domain": domain, "state": e["state"]})
        with self.lock:
            self.appliances, self.fetched = found, time.time()
        online = [k for k in found if self._online(k)]
        try:  # the LLM's persona lists these, so it can say [on: fan]
            config.HOME_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.HOME_DEVICES_FILE.write_text(", ".join(online))
        except OSError:
            pass
        return found

    def _fresh(self, max_age=REFRESH_S):
        if time.time() - self.fetched > max_age:
            self.refresh()
        return self.appliances

    def _call(self, domain, service, ids):
        self._request(f"/api/services/{domain}/{service}", {"entity_id": ids}, timeout=6)

    # ---- matching ----

    def match(self, text):
        """The appliances a request names. Returns (keys, problem): problem is a reply when it is unclear."""
        said = set(words(text))
        apps = self.appliances
        if ALL.search(text.lower()) or (re.search(r"\blights\b", text.lower()) and not any(
                set(k.split()) <= said and len(k.split()) > 1 for k in apps)):
            if "light" in said:
                keys = [k for k in apps if "light" in k.split()]
            elif "fan" in said:
                keys = [k for k in apps if "fan" in k.split()]
            else:
                keys = list(apps)
            return [k for k in keys if self._online(k)], None
        full = [k for k in apps if set(k.split()) <= said]
        if full:
            best = max(len(k.split()) for k in full)  # "desk light" beats "light"
            return [k for k in full if len(k.split()) == best], None
        near = self._fuzzy(text)
        if near:
            return [near], None
        partial = [k for k in apps if set(k.split()) & said & {"light", "fan"} and self._online(k)]
        if len(partial) == 1:
            return partial, None
        if partial:
            names = [f"the {k}" for k in partial]
            return [], f"Which one? {', '.join(names[:-1])} or {names[-1]}?"
        return [], None

    def _fuzzy(self, text):
        """Whisper's near-misses of a device name ("Disclate" for "desk light"), compared by their consonants,
        so "disclate" and "desklight" are both "dsklt". Letter-by-letter similarity scored that pair 0.35 and
        "bed light" vs "desk light" 0.71, which is backwards."""
        noise = {"turn", "switch", "put", "shut", "flip", "power", "toggle", "on", "off", "the", "my", "a", "please",
                 "is", "are", "all", "everything"}
        heard = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in noise]
        scores = []
        for k in self.appliances:
            target = sound(k)
            best = max((difflib.SequenceMatcher(None, sound("".join(heard[i:j])), target).ratio()
                        for i in range(len(heard)) for j in range(i + 1, min(len(heard), i + 3) + 1)), default=0)
            scores.append((best, k))
        scores.sort(reverse=True)
        if scores and scores[0][0] >= 0.8 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.15):
            return scores[0][1]
        return None

    def _online(self, key):
        return any(e["state"] not in ("unavailable", "unknown") for e in self.appliances.get(key, []))

    def _is_on(self, key):
        return any(e["state"] == "on" for e in self.appliances.get(key, []))

    # ---- what the router calls ----

    @staticmethod
    def claims(t):
        if re.match(r"^(how|why|what|when|where|who|can you tell)\b", t):
            return False
        return bool(ACTION.search(t) or QUERY.match(t))

    def handle(self, text):
        if not config.HA_TOKEN:
            return "Hmm,", "Home Assistant isn't set up yet. Its token goes in my dot env file."
        t = text.lower().strip(" .!?")
        try:
            self._fresh()
            keys, problem = self.match(t)
            if not keys and not problem:
                self.refresh()  # a device added or renamed since the last look
                keys, problem = self.match(t)
            if problem:
                return "Hmm,", problem
            if not keys:
                return "Hmm,", "I can't find that in Home Assistant."
            q = QUERY.match(t)
            if q and not ACTION.search(t):
                self.refresh()
                return self._report(keys)
            m = ACTION.search(t)
            want = m.group(2) or m.group(4) or ("off" if all(self._is_on(k) for k in keys) else "on")
            return self.switch(keys, want == "on")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return "Oh dear,", "Home Assistant didn't accept my token."
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            return "Oh dear,", "I couldn't reach Home Assistant just now."

    def spotted(self, name, on):
        """A command the keyword spotter heard (listen.CommandSpotter), when Whisper's text missed it."""
        try:
            self._fresh()
            if name not in self.appliances:
                return None
            return self.switch([name], on)
        except (urllib.error.URLError, TimeoutError, OSError):
            return "Oh dear,", "I couldn't reach Home Assistant just now."

    def names(self):
        return list(self.appliances)

    def switch(self, keys, on):
        """Switch whole appliances. Plugs go first and answer for the turn; lights behind a plug (a Bluetooth
        strip needs a moment to reconnect after power comes back) follow in the background."""
        offline = [k for k in keys if not self._online(k)]
        keys = [k for k in keys if k not in offline]
        if not keys:
            return "Hmm,", f"The {offline[0]} is offline in Home Assistant."
        service = "turn_on" if on else "turn_off"
        first, later = [], []
        for k in keys:
            ents = [e for e in self.appliances[k] if e["state"] not in ("unavailable", "unknown")]
            ents.sort(key=lambda e: e["domain"] != "switch")
            first.append(ents[0])
            later.extend(ents[1:])
        for domain in {e["domain"] for e in first}:
            self._call(domain, service, [e["id"] for e in first if e["domain"] == domain])
        if later:
            threading.Thread(target=self._follow, args=(service, later), daemon=True).start()
        for k in keys:
            for e in self.appliances[k]:
                if e["state"] not in ("unavailable", "unknown"):
                    e["state"] = "on" if on else "off"
        names = self._names(keys)
        verb = "is" if len(keys) == 1 else "are"
        return ("Sure thing." if on else "Okay,", f"{names[0].upper() + names[1:]} {verb} {'on' if on else 'off'}.",
                self.card(keys))

    def _follow(self, service, ents):
        time.sleep(0.6 if service == "turn_on" else 0)
        for e in ents:
            try:
                self._call(e["domain"], service, [e["id"]])
            except Exception as err:  # the plug already did the job; log and move on
                print(json.dumps({"event": "home_follow_error", "entity": e["id"], "error": str(err)}), flush=True)

    def _report(self, keys):
        on = [k for k in keys if self._is_on(k)]
        off = [k for k in keys if k not in on]
        parts = []
        if on:
            parts.append(f"{self._names(on)} {'is' if len(on) == 1 else 'are'} on")
        if off:
            parts.append(f"{self._names(off)} {'is' if len(off) == 1 else 'are'} off")
        s = " and ".join(parts)
        return "Right,", s[0].upper() + s[1:] + ".", self.card(keys)

    @staticmethod
    def _names(keys):
        names = [f"the {k}" for k in keys]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

    def card(self, keys):
        return {"kind": "home", "devices": [
            {"name": k[0].upper() + k[1:], "icon": "fan" if "fan" in k.split() else "light" if "light" in k.split() else "plug",
             "on": self._is_on(k), "online": self._online(k)} for k in keys]}

    def everything_off(self):
        """For "good night": every light and fan that is on."""
        self._fresh(30)
        keys = [k for k in self.appliances if self._is_on(k)]
        return self.switch(keys, False) if keys else None
