"""Jansky's list: reminders and to-dos, kept in a plain Markdown checklist and shown on the screen.

"Remind me to buy a pen today" adds a line. A reminder with a time ("at 5 pm", "in 20 minutes") is also
spoken when it is due, and stays on the list until it is ticked off, by touch or by voice. The list stays on
the screen until it is closed by touch; a small badge keeps the count while it is closed. The file survives
restarts (due times are kept in a comment on the line) and can be edited by hand.
"""
import hashlib
import re
import threading
import time

from . import config

HEADER = """# Jansky's list

One item per line. Jansky adds lines ("remind me to ..."), ticks them off ("I bought the pen", or a tap on
the screen), and shows them on its face. Edit freely.

"""
LINE = re.compile(r"^- \[( |x)\] (.+?)(?: · (.+?))?(?: <!-- due (\d+) -->)?\s*$")


def item_id(text):
    return hashlib.sha1(text.lower().encode()).hexdigest()[:8]


class TodoList:
    def __init__(self, announce, on_change=None):
        self.announce = announce    # called with text when a timed reminder is due
        self.on_change = on_change  # called with (items, shown, highlight) for the face
        self.lock = threading.Lock()
        self.timers = {}            # id -> threading.Timer
        self.fired = set()          # due reminders, highlighted until ticked off
        self.items = self._load()
        self.shown = bool(self.items)  # pending items are on screen at start-up: a morning glance
        now = time.time()
        for it in self.items:
            if it["due"] and it["due"] > now:
                self._schedule(it)
            elif it["due"]:
                self.fired.add(it["id"])

    # ---- the file ----

    def _load(self):
        if not config.TODO_FILE.exists():
            return []
        items = []
        for line in config.TODO_FILE.read_text().splitlines():
            m = LINE.match(line)
            if m and m.group(1) == " ":
                items.append({"id": item_id(m.group(2)), "text": m.group(2), "when": m.group(3) or "",
                              "due": int(m.group(4)) if m.group(4) else None})
        return items

    def _save(self):
        lines = [f"- [ ] {it['text']}" + (f" · {it['when']}" if it["when"] else "")
                 + (f" <!-- due {it['due']} -->" if it["due"] else "") for it in self.items]
        config.TODO_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TODO_FILE.write_text(HEADER + "\n".join(lines) + ("\n" if lines else ""))

    # ---- changes ----

    def add(self, text, when="", due=None):
        with self.lock:
            iid = item_id(text)
            self.items = [it for it in self.items if it["id"] != iid]  # saying it twice moves it to the top
            it = {"id": iid, "text": text, "when": when, "due": int(due) if due else None}
            self.items.insert(0, it)
            self._save()
            if it["due"]:
                self._schedule(it)
        self.show(highlight=iid)
        return it

    def done(self, iid):
        with self.lock:
            hit = [it for it in self.items if it["id"] == iid]
            self.items = [it for it in self.items if it["id"] != iid]
            if hit:
                self._save()
                self.fired.discard(iid)
                t = self.timers.pop(iid, None)
                if t:
                    t.cancel()
        self._changed()
        return hit[0] if hit else None

    def clear(self):
        with self.lock:
            n = len(self.items)
            for t in self.timers.values():
                t.cancel()
            self.items, self.timers = [], {}
            self.fired.clear()
            self._save()
        self._changed()
        return n

    def find(self, text):
        """The item that shares the most words with what was said ("I bought the pen" -> "Buy a pen")."""
        said = set(stems(text))
        best, score = None, 0
        for it in self.items:
            s = len(said & set(stems(it["text"])))
            if s > score:
                best, score = it, s
        return best

    def show(self, highlight=None):
        self.shown = True
        self._changed(highlight)

    def hide(self):
        self.shown = False
        self._changed()

    def snapshot(self):
        with self.lock:
            return [{**it, "fired": it["id"] in self.fired} for it in self.items]

    def _changed(self, highlight=None):
        if self.on_change:
            self.on_change(self.snapshot(), self.shown and bool(self.items), highlight)

    # ---- timed reminders ----

    def _schedule(self, it):
        t = threading.Timer(max(0, it["due"] - time.time()), self._fire, args=(it["id"],))
        t.daemon = True
        self.timers[it["id"]] = t
        t.start()

    def _fire(self, iid):
        with self.lock:
            self.timers.pop(iid, None)
            hit = [it for it in self.items if it["id"] == iid]
            if hit:
                self.fired.add(iid)
        if hit:
            self.show(highlight=iid)
            self.announce(f"Reminder: {hit[0]['text'][0].lower() + hit[0]['text'][1:]}.")


STOP = {"the", "a", "an", "my", "to", "for", "of", "and", "i", "ive", "have", "it", "that", "list", "off", "from",
        "already", "just", "done", "mark", "tick", "cross", "remove", "delete", "please"}
VERB_FORMS = {"bought": "buy", "buying": "buy", "got": "get", "getting": "get", "called": "call", "calling": "call",
              "picked": "pick", "picking": "pick", "sent": "send", "sending": "send", "paid": "pay", "paying": "pay",
              "took": "take", "taking": "take", "did": "do", "doing": "do", "made": "make", "making": "make",
              "booked": "book", "booking": "book", "fed": "feed", "feeding": "feed", "emailed": "email",
              "emailing": "email", "cleaned": "clean", "cleaning": "clean", "watered": "water", "watering": "water",
              "finished": "finish", "finishing": "finish", "checked": "check", "checking": "check",
              "ordered": "order", "ordering": "order", "texted": "text", "texting": "text", "wrote": "write",
              "writing": "write", "returned": "return", "returning": "return"}


def stems(text):
    out = []
    for w in re.findall(r"[a-z']+", text.lower()):
        w = VERB_FORMS.get(w, w.replace("'", ""))
        if w not in STOP:
            out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out
