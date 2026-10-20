#!/usr/bin/env python3
"""
PRD Council: Claude, GPT and DeepSeek debate a PRD until all three approve.

  Claude   : runs through the Claude Code CLI  (uses your Claude Pro plan)
  GPT      : runs through the Codex CLI        (uses your ChatGPT Plus plan)
  DeepSeek : runs through OpenRouter free tier (or DeepSeek's own API)

Flow:
  1. The editor (Claude by default) writes PRD v1 from your idea.
  2. All three models review it independently, each with its own focus,
     and return a verdict (approve / changes) plus a list of issues.
  3. The editor revises the PRD and writes a resolution log saying how each
     issue was handled (accepted, partially accepted, rejected with reason).
  4. Repeat until all three approve, or the round limit is hit.

Outputs (in prd_runs/<timestamp>/):
  PRD.md          the final PRD
  ISSUES_LOG.md   every issue raised, who raised it, and how it was resolved
  rounds/         raw drafts and reviews for every round

Usage:
  python prd_council.py idea.txt
  python prd_council.py "A habit tracker app for students" --rounds 3
  python prd_council.py --prd my_prd.md   # start from your own PRD
  python prd_council.py --check        # test that all three models respond
  python prd_council.py idea.txt --mock  # dry run, uses no quota
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import zipfile
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------------------
# Configuration (override any of these with environment variables)
# ----------------------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")      # empty = Claude Code default
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")        # empty = Codex default
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")  # optional, used instead of OpenRouter
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "")  # empty = auto pick a free DeepSeek model
CLI_TIMEOUT = int(os.environ.get("COUNCIL_TIMEOUT", "900"))

MEMBERS = {
    "claude": {
        "label": "Claude",
        "focus": (
            "ARCHITECTURE AND COHERENCE: Is the technical approach sound and buildable? "
            "Are requirements consistent with each other, testable, and specific enough "
            "that an AI coding agent could implement them without guessing? Is the data "
            "model complete? Are integrations, auth, and state handling covered?"
        ),
    },
    "gpt": {
        "label": "GPT",
        "focus": (
            "PRODUCT AND USER VALUE: Is the problem clearly defined? Are the target users, "
            "user stories, and user flows complete? Is the MVP scope right (not bloated, "
            "not missing something essential)? Are success metrics measurable? Is the UX "
            "described well enough to build?"
        ),
    },
    "deepseek": {
        "label": "DeepSeek",
        "focus": (
            "RISKS, EDGE CASES, AND FEASIBILITY: What can go wrong? Missing edge cases, "
            "error handling, security and privacy gaps, performance and scaling limits, "
            "cost surprises, third party dependency risks, and unrealistic milestones."
        ),
    },
}

# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------
PRD_SECTIONS = """1. Overview and Problem Statement
2. Goals and Non Goals
3. Target Users and Personas
4. User Stories and Key User Flows
5. Functional Requirements (numbered FR1, FR2, ... each testable)
6. Non Functional Requirements (performance, security, privacy, reliability)
7. Scope: MVP vs Later Phases
8. Technical Approach (architecture, suggested stack, data model, integrations)
9. Edge Cases and Error Handling
10. Risks and Mitigations
11. Success Metrics
12. Milestones
13. Open Questions (things only the product owner can decide)"""

NO_TOOLS = (
    "Do not use any tools, do not read or write files, and do not run commands. "
    "Answer only with text, based only on what is in this message."
)

DRAFT_PROMPT = """You are a senior product manager and software architect.
{no_tools}

Write a complete, implementation ready Product Requirements Document (PRD) in Markdown
for the idea below. The PRD will be handed to an AI coding agent (Claude Code) to build,
so be specific and unambiguous. Use exactly these sections:

{sections}

If something important is unclear in the idea, make a sensible assumption, state it
explicitly, and list it under Open Questions.

Output ONLY the PRD Markdown, starting with a level 1 heading.

=== IDEA ===
{idea}
"""

ADJUST_PROMPT = """You are a senior product manager and software architect.
{no_tools}

Below is a PRD written by the product owner. Your job is to check it and improve it,
NOT to rewrite it from scratch. The PRD will be handed to an AI coding agent (Claude Code)
to build, so it must be specific and unambiguous.

1. Keep the owner's product decisions, scope, wording and structure wherever they are sound.
   Their decisions are authoritative unless they are clearly contradictory or unbuildable.
2. Fix contradictions, vague or untestable requirements, and missing details.
3. Make sure it covers all of these topics (add any that are missing, as new sections or
   inside existing ones, without renaming the owner's sections unnecessarily):

{sections}

4. Where you had to assume something, state the assumption and add it to Open Questions.
5. At the very end, add a section "## Changes From Original" listing what you changed and why.

Output ONLY the improved PRD Markdown.

=== OWNER'S PRD ===
{idea}
"""

REVIEW_PROMPT = """You are {label}, one of three independent reviewers on a PRD review council.
{no_tools}

Your primary focus: {focus}
You may also flag anything else that is clearly wrong or missing.

Rules:
1. Be rigorous. Approve only if you would hand this PRD to an engineering team today.
2. Do not approve just to be agreeable, and do not invent problems to look thorough.
3. If the editor rejected one of your earlier issues and their reason is sound, drop it.
   If their reason is weak, raise it again and explain why.
4. Severity: "critical" (would cause a wrong or broken product), "major" (significant gap),
   "minor" (polish). You may only give verdict "approve" if you have no critical or major issues.

=== ORIGINAL IDEA ===
{idea}

=== PRD (round {round_no}) ===
{prd}

=== EDITOR'S RESOLUTION LOG FROM THE PREVIOUS ROUND ===
{resolution}

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "verdict": "approve" or "changes",
  "summary": "one or two sentences on overall quality",
  "issues": [
    {{"severity": "critical|major|minor", "section": "section name",
      "issue": "what is wrong or missing", "suggestion": "concrete fix"}}
  ]
}}
"""

REVISE_PROMPT = """You are the editor of a PRD that a three model review council just reviewed.
{no_tools}

Revise the PRD to address the reviewers' issues. For each issue decide:
  accepted  : you changed the PRD to fix it
  partial   : you addressed part of it (explain)
  rejected  : you did not change the PRD (give a clear, honest reason)
Reject an issue only when it is genuinely wrong, out of scope, or conflicts with the idea.
Keep the same section structure. Keep everything that was already good.

=== ORIGINAL IDEA ===
{idea}

=== CURRENT PRD ===
{prd}

=== REVIEWS ===
{reviews}

Respond in EXACTLY this format, with these markers on their own lines:

<<<PRD>>>
(the full revised PRD in Markdown)
<<<END_PRD>>>
<<<LOG>>>
[{{"reviewer": "Claude|GPT|DeepSeek", "issue": "short restatement",
   "decision": "accepted|partial|rejected", "note": "what you changed or why not"}}]
<<<END_LOG>>>
"""

# ----------------------------------------------------------------------------
# Model callers
# ----------------------------------------------------------------------------
class CouncilError(RuntimeError):
    pass


def _which(name):
    path = shutil.which(name)
    if not path:
        raise CouncilError(
            f"'{name}' was not found on your PATH. Install it and log in first "
            f"(see README.md)."
        )
    return path


def _empty_workdir():
    # Run CLIs in an empty folder so they never see your codebase.
    return tempfile.mkdtemp(prefix="prd_council_")


def call_claude(prompt):
    cmd = [_which("claude"), "-p", "Follow the instructions given on standard input exactly."]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    workdir = _empty_workdir()
    try:
        res = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=workdir, timeout=CLI_TIMEOUT,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise CouncilError(f"Claude Code failed: {res.stderr.strip()[:500]}")
    return res.stdout.strip()


def call_gpt(prompt):
    workdir = _empty_workdir()
    out_file = os.path.join(workdir, "last_message.txt")
    cmd = [
        _which("codex"), "exec", "--skip-git-repo-check",
        "--sandbox", "read-only", "-o", out_file,
    ]
    if CODEX_MODEL:
        cmd += ["-m", CODEX_MODEL]
    cmd.append("-")  # read prompt from stdin
    try:
        res = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=workdir, timeout=CLI_TIMEOUT,
        )
        text = ""
        if os.path.exists(out_file):
            text = Path(out_file).read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            text = res.stdout.strip()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if not text:
        raise CouncilError(f"Codex CLI failed: {res.stderr.strip()[:500]}")
    return text


_resolved_deepseek_model = None


def _pick_free_deepseek_model():
    """Free model IDs on OpenRouter change over time, so look up the current ones."""
    global _resolved_deepseek_model
    if DEEPSEEK_MODEL:
        return DEEPSEEK_MODEL
    if _resolved_deepseek_model:
        return _resolved_deepseek_model
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as r:
            models = json.load(r).get("data", [])
    except Exception as e:
        raise CouncilError(f"Could not list OpenRouter models: {e}. Set DEEPSEEK_MODEL manually.")
    ids = {m.get("id", "") for m in models}
    free = [m for m in models
            if "deepseek/" in m.get("id", "") and m["id"].endswith(":free")]
    if free:
        free.sort(key=lambda m: m.get("created", 0), reverse=True)  # newest first
        _resolved_deepseek_model = free[0]["id"]
        print(f"   (using free OpenRouter model {_resolved_deepseek_model})")
        return _resolved_deepseek_model
    # No free DeepSeek right now: fall back to the cheapest paid DeepSeek model.
    for candidate in ("~deepseek/deepseek-flash-latest", "deepseek/deepseek-chat"):
        if candidate in ids:
            _resolved_deepseek_model = candidate
            print(f"   (no free DeepSeek on OpenRouter right now; using {candidate}, "
                  f"which costs well under 1 cent per run and needs a few dollars of credit)")
            return candidate
    paid = sorted((m for m in models if "deepseek/" in m.get("id", "")),
                  key=lambda m: float(m.get("pricing", {}).get("completion", 1) or 1))
    if not paid:
        raise CouncilError("No DeepSeek model found on OpenRouter. Set DEEPSEEK_MODEL manually.")
    _resolved_deepseek_model = paid[0]["id"]
    print(f"   (no free DeepSeek on OpenRouter right now; using {_resolved_deepseek_model})")
    return _resolved_deepseek_model


def _post_json(url, headers, payload, retries=5):
    data = json.dumps(payload).encode("utf-8")
    delay = 10
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (429, 502, 503) and attempt < retries - 1:
                print(f"   (DeepSeek busy or rate limited, retrying in {delay}s)")
                time.sleep(delay)
                delay *= 2
                continue
            hints = {
                401: "Your API key was rejected. Check it is copied fully, and that you set it "
                     "in the SAME terminal you run the script from (or opened a new terminal after setx).",
                402: "Your OpenRouter account has no credits. Add a few dollars at "
                     "https://openrouter.ai/settings/credits (a full run costs well under 1 cent).",
                404: "Model not available. If the message mentions 'data policy', open "
                     "https://openrouter.ai/settings/privacy and allow the providers/training "
                     "settings it asks for. Otherwise set DEEPSEEK_MODEL to a model that exists.",
            }
            hint = hints.get(e.code, "")
            raise CouncilError(f"DeepSeek request failed ({e.code}): {body}\n   Fix: {hint}"
                               if hint else f"DeepSeek request failed ({e.code}): {body}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            raise CouncilError(f"DeepSeek request failed: {e}")
    raise CouncilError("DeepSeek request failed after retries.")


def call_deepseek(prompt):
    if DEEPSEEK_KEY:
        url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
        model = DEEPSEEK_MODEL or "deepseek-chat"
    elif OPENROUTER_KEY:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "X-Title": "PRD Council",
        }
        model = _pick_free_deepseek_model()
    else:
        raise CouncilError("Set OPENROUTER_API_KEY (free) or DEEPSEEK_API_KEY.")
    resp = _post_json(url, headers, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        text = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise CouncilError(f"Unexpected DeepSeek response: {str(resp)[:300]}")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise CouncilError("DeepSeek returned an empty response.")
    return text


# ----------------------------------------------------------------------------
# Mock mode (tests the whole flow without using any quota)
# ----------------------------------------------------------------------------
_mock_state = {"reviews": 0}


def call_mock(member):
    def _call(prompt):
        if "<<<PRD>>>" in prompt:
            return ("<<<PRD>>>\n# Mock PRD (revised)\n\n## 1. Overview\nRevised.\n<<<END_PRD>>>\n"
                    "<<<LOG>>>\n[{\"reviewer\": \"GPT\", \"issue\": \"metrics vague\", "
                    "\"decision\": \"accepted\", \"note\": \"added targets\"}]\n<<<END_LOG>>>")
        if '"verdict"' in prompt:
            _mock_state["reviews"] += 1
            if _mock_state["reviews"] <= 3:
                return json.dumps({"verdict": "changes", "summary": "Needs work.",
                                   "issues": [{"severity": "major", "section": "Success Metrics",
                                               "issue": f"{member} mock issue",
                                               "suggestion": "add numbers"}]})
            return "```json\n" + json.dumps({"verdict": "approve", "summary": "Good.",
                                            "issues": []}) + "\n```"
        return "# Mock PRD\n\n## 1. Overview\nDraft."
    return _call


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
def parse_json_block(text):
    """Pull the first JSON object or array out of a model response."""
    text = re.sub(r"```(?:json)?", "", text)
    pairs = [("{", "}"), ("[", "]")]
    # Try whichever bracket appears first, so a list of objects stays a list.
    pairs.sort(key=lambda p: text.find(p[0]) if text.find(p[0]) != -1 else len(text))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def parse_review(text, label):
    data = parse_json_block(text)
    if not isinstance(data, dict):
        # Treat unparseable output as a single major issue so nothing is lost.
        return {"verdict": "changes", "summary": "(response was not valid JSON)",
                "issues": [{"severity": "major", "section": "General",
                            "issue": text[:2000], "suggestion": ""}], "raw": text}
    issues = data.get("issues") or []
    verdict = str(data.get("verdict", "changes")).lower()
    blocking = [i for i in issues if str(i.get("severity", "")).lower() in ("critical", "major")]
    if blocking:
        verdict = "changes"  # cannot approve with critical or major issues
    return {"verdict": verdict, "summary": data.get("summary", ""), "issues": issues}


def parse_revision(text, fallback_prd):
    prd_match = re.search(r"<<<PRD>>>\s*(.*?)\s*<<<END_PRD>>>", text, re.S)
    log_match = re.search(r"<<<LOG>>>\s*(.*?)\s*<<<END_LOG>>>", text, re.S)
    prd = prd_match.group(1).strip() if prd_match else ""
    if not prd:
        print("   (warning: editor did not use the PRD markers; keeping previous PRD)")
        prd = fallback_prd
    log = parse_json_block(log_match.group(1)) if log_match else None
    return prd, log if isinstance(log, list) else []


def format_reviews(reviews):
    parts = []
    for key, r in reviews.items():
        label = MEMBERS[key]["label"]
        lines = [f"--- {label} (verdict: {r['verdict']}) ---", f"Summary: {r['summary']}"]
        for n, i in enumerate(r["issues"], 1):
            lines.append(f"{n}. [{i.get('severity', '?')}] {i.get('section', '')}: "
                         f"{i.get('issue', '')} | Suggestion: {i.get('suggestion', '')}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def run_council(idea, rounds, editor, out_dir, callers, from_prd=False):
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ("original_prd.md" if from_prd else "idea.txt")).write_text(idea, encoding="utf-8")
    ed_label = MEMBERS[editor]["label"]

    def ask(member, prompt, what):
        print(f"   {MEMBERS[member]['label']}: {what}...", flush=True)
        t0 = time.time()
        out = callers[member](prompt)
        print(f"   {MEMBERS[member]['label']}: done in {time.time() - t0:.0f}s", flush=True)
        return out

    if from_prd:
        print(f"\n== Checking and adjusting your PRD ({ed_label}) ==")
        prd = ask(editor, ADJUST_PROMPT.format(no_tools=NO_TOOLS, sections=PRD_SECTIONS, idea=idea),
                  "reviewing and improving your PRD")
        # Reviewers treat the owner's PRD as the source of truth for intent.
        idea = ("(The product owner supplied their own PRD. Treat its product decisions as "
                "authoritative; the version below is the editor's improved copy.)\n\n" + idea)
    else:
        print(f"\n== Drafting PRD v1 ({ed_label}) ==")
        prd = ask(editor, DRAFT_PROMPT.format(no_tools=NO_TOOLS, sections=PRD_SECTIONS, idea=idea),
                  "writing first draft")
    (rounds_dir / "prd_v1.md").write_text(prd, encoding="utf-8")

    resolution = "(none, this is the first review round)"
    history = []
    consensus = False
    last_reviews = {}

    for rnd in range(1, rounds + 1):
        print(f"\n== Round {rnd}: independent reviews ==")
        reviews = {}
        for key in MEMBERS:
            prompt = REVIEW_PROMPT.format(
                label=MEMBERS[key]["label"], focus=MEMBERS[key]["focus"], no_tools=NO_TOOLS,
                idea=idea, round_no=rnd, prd=prd, resolution=resolution,
            )
            try:
                raw = ask(key, prompt, "reviewing")
            except CouncilError as e:
                print(f"   {MEMBERS[key]['label']} unavailable this round: {e}")
                continue
            (rounds_dir / f"round{rnd}_review_{key}.txt").write_text(raw, encoding="utf-8")
            reviews[key] = parse_review(raw, MEMBERS[key]["label"])
            v = reviews[key]
            print(f"   -> {MEMBERS[key]['label']}: {v['verdict'].upper()} "
                  f"({len(v['issues'])} issues)")

        if not reviews:
            raise CouncilError("No reviewer responded this round.")
        last_reviews = reviews

        if len(reviews) == len(MEMBERS) and all(r["verdict"] == "approve" for r in reviews.values()):
            consensus = True
            print(f"\n== Consensus reached in round {rnd}: all three approve ==")
            history.append({"round": rnd, "reviews": reviews, "log": []})
            break

        print(f"\n== Round {rnd}: {ed_label} revises ==")
        raw = ask(editor, REVISE_PROMPT.format(no_tools=NO_TOOLS, idea=idea, prd=prd,
                                               reviews=format_reviews(reviews)), "revising")
        (rounds_dir / f"round{rnd}_revision_raw.txt").write_text(raw, encoding="utf-8")
        prd, log = parse_revision(raw, prd)
        (rounds_dir / f"prd_v{rnd + 1}.md").write_text(prd, encoding="utf-8")
        history.append({"round": rnd, "reviews": reviews, "log": log})
        resolution = json.dumps(log, indent=2) if log else "(editor gave no log)"

    write_outputs(out_dir, prd, history, consensus, last_reviews, rounds)
    return consensus


def write_outputs(out_dir, prd, history, consensus, last_reviews, rounds):
    (out_dir / "PRD.md").write_text(prd, encoding="utf-8")

    lines = ["# PRD Council: Issues Log", ""]
    if consensus:
        lines.append(f"**Result: consensus.** All three models approved in round {history[-1]['round']}.")
    else:
        lines.append(f"**Result: no full consensus after {rounds} rounds.** "
                     "See unresolved objections at the bottom.")
    lines.append("")

    for h in history:
        lines += [f"## Round {h['round']}", ""]
        for key, r in h["reviews"].items():
            label = MEMBERS[key]["label"]
            lines.append(f"### {label}: {r['verdict']}")
            if r["summary"]:
                lines.append(f"_{r['summary']}_")
            lines.append("")
            for i in r["issues"]:
                lines.append(f"* **[{i.get('severity', '?')}] {i.get('section', '')}**: "
                             f"{i.get('issue', '')}  ")
                if i.get("suggestion"):
                    lines.append(f"  Suggestion: {i['suggestion']}")
            lines.append("")
        if h["log"]:
            lines += ["### Editor's resolutions", "",
                      "| Reviewer | Issue | Decision | Note |", "|---|---|---|---|"]
            for e in h["log"]:
                cells = [str(e.get(k, "")).replace("|", "/").replace("\n", " ")
                         for k in ("reviewer", "issue", "decision", "note")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

    if not consensus:
        lines += ["## Unresolved objections (final round)", ""]
        for key, r in last_reviews.items():
            if r["verdict"] != "approve":
                for i in r["issues"]:
                    if str(i.get("severity", "")).lower() in ("critical", "major"):
                        lines.append(f"* {MEMBERS[key]['label']}: {i.get('issue', '')}")
        lines.append("")

    (out_dir / "ISSUES_LOG.md").write_text("\n".join(lines), encoding="utf-8")


def _docx_to_text(path):
    """Extract plain text from a Word .docx file using only the standard library."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)          # paragraph ends become newlines
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_input_file(path):
    """Read an idea or PRD file, whatever format or encoding it was saved in."""
    path = Path(path)
    raw = path.read_bytes()

    if raw[:4] == b"PK\x03\x04":  # zip container: Word .docx, or Pages
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            names = []
        if "word/document.xml" in names:
            print(f"   (note: {path.name} is a Word document; extracting its text)")
            return _docx_to_text(path)
        raise CouncilError(
            f"{path.name} looks like a Pages or other packaged document, not text. "
            "Open it and use File > Export To > Plain Text (or Word), then try again."
        )
    if raw[:5] == b"%PDF-":
        raise CouncilError(
            f"{path.name} is a PDF. Copy its text into a .md or .txt file, then try again."
        )
    if raw[:5] == b"{\\rtf":
        raise CouncilError(
            f"{path.name} is a Rich Text (RTF) file. In TextEdit choose Format > "
            "Make Plain Text, save, then try again."
        )

    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "mac_roman"):
        try:
            text = raw.decode(enc)
            if enc != "utf-8-sig":
                print(f"   (note: {path.name} is not UTF-8; read it as {enc})")
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")  # never fails


def check_members(callers):
    ok = True
    for key, fn in callers.items():
        label = MEMBERS[key]["label"]
        try:
            out = fn("Reply with exactly the word OK and nothing else.")
            print(f"  {label:9} OK   -> {' '.join(out.split())[:60]}")
        except Exception as e:
            ok = False
            print(f"  {label:9} FAIL -> {e}")
    return ok


def main():
    p = argparse.ArgumentParser(description="Claude, GPT and DeepSeek debate a PRD to consensus.")
    p.add_argument("idea", nargs="?", help="Path to a text/markdown file with your idea, or the idea itself in quotes")
    p.add_argument("--prd", metavar="FILE",
                   help="Start from your existing PRD file: Claude checks and improves it, then the council debates it")
    p.add_argument("--rounds", type=int, default=3, help="Maximum review rounds (default 3)")
    p.add_argument("--editor", choices=list(MEMBERS), default="claude",
                   help="Which model writes and revises the PRD (default claude)")
    p.add_argument("--out", default="prd_runs", help="Output folder (default prd_runs)")
    p.add_argument("--check", action="store_true", help="Test that all three models respond, then exit")
    p.add_argument("--mock", action="store_true", help="Dry run with fake responses (no quota used)")
    args = p.parse_args()

    if args.mock:
        callers = {k: call_mock(k) for k in MEMBERS}
    else:
        callers = {"claude": call_claude, "gpt": call_gpt, "deepseek": call_deepseek}

    if args.check:
        print("Checking council members...")
        sys.exit(0 if check_members(callers) else 1)

    from_prd = bool(args.prd)
    if from_prd:
        if args.idea:
            p.error("give either an idea or --prd, not both")
        prd_path = Path(args.prd)
        if not prd_path.is_file():
            p.error(f"PRD file not found: {args.prd}")
        try:
            idea = read_input_file(prd_path)
        except CouncilError as e:
            p.error(str(e))
    else:
        if not args.idea:
            p.error("give an idea file, a quoted idea, or --prd FILE (or use --check)")
        idea_path = Path(args.idea)
        try:
            idea = read_input_file(idea_path) if idea_path.is_file() else args.idea
        except CouncilError as e:
            p.error(str(e))
    if len(idea.strip()) < 10:
        p.error("the input is too short; describe it in at least a sentence or two")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / stamp
    try:
        consensus = run_council(idea, args.rounds, args.editor, out_dir, callers, from_prd)
    except CouncilError as e:
        print(f"\nStopped: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped by user. Partial results are in", out_dir)
        sys.exit(130)

    print(f"\nDone. {'Consensus reached.' if consensus else 'No full consensus; see ISSUES_LOG.md.'}")
    print(f"  Final PRD : {out_dir / 'PRD.md'}")
    print(f"  Issues log: {out_dir / 'ISSUES_LOG.md'}")


if __name__ == "__main__":
    main()
