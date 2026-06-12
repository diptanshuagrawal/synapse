#!/usr/bin/env python3
"""ticketize_reply.py — deterministic parser for Slack approval replies (/ticketize v1.5b).

NO LLM, NO network. Turns a free-text owner reply ("apply C1, G2", "reject C3",
"approve all except C1") into an explicit per-label decision map. FAIL-CLOSED: if the
reply is ambiguous or references unknown labels, `ok=false` and NOTHING should be applied
— the skill must then ask the owner to rephrase rather than guess. This gates Jira writes.

Usage:
  echo '{"reply":"apply C1, G2; reject C3","labels":["C1","C2","C3","G1","G2"]}' \
    | python3 bin/ticketize_reply.py
  -> {"ok":true,"decisions":{"C1":"approve","G2":"approve","C3":"reject"},
      "approve_all":false,"reject_all":false,"ambiguous":[],"unknown":[]}

Grammar (case-insensitive, left-to-right; a verb sets the action for labels that follow):
  approve verbs : approve apply create accept ship go do yes ok okay 👍 ✅
  reject  verbs : reject skip drop cancel ignore no except 👎 ❌
  scope word    : all / everything  (with current action -> approve_all/reject_all)
  labels        : C<n> / G<n>  (the candidate ids from the proposal file)
  "except"/"but not"/"not" flips subsequent labels to reject (the common "approve all
  except C1" case). A label appearing before ANY verb is AMBIGUOUS -> fail closed.
"""
import sys, json, re

APPROVE = {"approve", "apply", "create", "accept", "ship", "go", "do", "yes", "ok", "okay", "👍", "✅"}
REJECT = {"reject", "skip", "drop", "cancel", "ignore", "no", "except", "👎", "❌"}
SCOPE = {"all", "everything", "rest"}
LABEL_RE = re.compile(r"\b([cg]\d{1,3})\b", re.I)
WORD_RE = re.compile(r"[a-z👍✅👎❌]+|\b[cg]\d{1,3}\b", re.I)


def parse(reply, labels):
    valid = {l.upper() for l in labels}
    decisions = {}
    approve_all = reject_all = False
    ambiguous, unknown = [], []
    action = None  # None until a verb is seen

    # tokenize: keep words, emojis, and label tokens
    tokens = re.findall(r"👍|✅|👎|❌|[A-Za-z]+\d{0,3}|\d+", reply)
    for tok in tokens:
        low = tok.lower()
        if low in APPROVE or tok in APPROVE:
            action = "approve"; continue
        if low in REJECT or tok in REJECT:
            action = "reject"; continue
        if low in SCOPE:
            if action == "approve":
                approve_all = True
            elif action == "reject":
                reject_all = True
            continue
        m = re.fullmatch(r"[cg]\d{1,3}", low)
        if m:
            lab = tok.upper()
            if lab not in valid:
                unknown.append(lab); continue
            if action is None:
                ambiguous.append(lab); continue
            decisions[lab] = action
            continue
        # other words ignored (connectors: the, and, please, them, etc.)

    ok = (approve_all or reject_all or bool(decisions)) and not ambiguous and not unknown
    return {
        "ok": ok,
        "decisions": decisions,
        "approve_all": approve_all,
        "reject_all": reject_all,
        "ambiguous": ambiguous,   # labels before any verb -> can't tell intent
        "unknown": unknown,       # candidate-shaped ids not in this run's label set
    }


def main():
    try:
        inp = json.load(sys.stdin)
        reply = inp["reply"]
        labels = inp.get("labels", [])
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad input: {e}"}))
        sys.exit(1)
    json.dump(parse(reply, labels), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
