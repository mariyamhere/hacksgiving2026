"""Turn findings into a roast. The model only writes jokes about findings we
pass it, and we render every link ourselves, so nothing is invented."""
import json

import llm

SYSTEM = """You are Roast My Repo: a stand-up comedian who is also a brutally experienced senior engineer.
You roast GitHub repos. Rules:
- You may ONLY joke about the findings provided. Never invent problems, files, numbers or people.
- Each burn: one or two sentences, max 40 words, punchy, specific to its evidence. Quote numbers from the evidence.
- Roast the code, never the person: no insults about identity, appearance, intelligence or worth. Affectionate savagery.
- For each burn also give a one-line "fix": the concrete, genuinely useful thing to do about it.
- Pick the 5-8 funniest/most important findings. Order them for comedic rhythm, save the best for last.
Reply with JSON only:
{"opener": "<one line opener about the repo>",
 "burns": [{"id": "<finding id>", "burn": "...", "fix": "..."}],
 "closer": "<one line sign-off that is secretly encouraging>"}"""


def heat(findings):
    """Crispiness 0-10 from severities. Deterministic, so the model can't fudge it."""
    score = sum({1: 0.6, 2: 1.3, 3: 2.2}[f["severity"]] for f in findings)
    return max(1, min(10, round(score)))


def roast(insp):
    meta, findings = insp.meta, insp.findings
    if not findings:
        return (f"## 🔥 Roast of `{insp.full}`\n\nI came here to roast and I found... nothing. "
                f"Tests, docs, hygiene, all fine. This is the most disappointing day of my career. 10/10, no notes.\n")
    brief = [{k: f[k] for k in ("id", "severity", "title", "evidence")} for f in findings]
    context = {
        "repo": insp.full, "description": meta.get("description"), "language": meta.get("language"),
        "stars": meta.get("stargazers_count"), "open_issues": meta.get("open_issues_count"),
        "stats": insp.stats,
    }
    raw = llm.chat(SYSTEM, f"REPO:\n{json.dumps(context)}\n\nFINDINGS_JSON:{json.dumps(brief)}",
                   max_tokens=1500, json_mode=True)
    data = _parse(raw)
    by_id = {f["id"]: f for f in findings}
    burns = [b for b in data.get("burns", []) if b.get("id") in by_id]
    if not burns:  # model went off-script: fall back to plain findings
        burns = [{"id": f["id"], "burn": f["evidence"], "fix": ""} for f in findings[:6]]

    h = heat(findings)
    out = [f"## 🔥 Roast of `{insp.full}`",
           f"**Crispiness:** {'🔥' * h}{'▫️' * (10 - h)} **{h}/10**",
           "", f"_{data.get('opener', '').strip()}_", ""]
    for n, b in enumerate(burns, 1):
        f = by_id[b["id"]]
        link = f["url"] or f.get("issue_url")
        title = f"[{f['title']}]({link})" if link else f["title"]
        out.append(f"**{n}. {title}**  \n{b['burn'].strip()}")
        if b.get("fix"):
            out.append(f"> 🛠️ {b['fix'].strip()}")
        out.append("")
    out.append(f"_{data.get('closer', '').strip()}_")
    rest = [f for f in findings if f["id"] not in {b['id'] for b in burns}]
    if rest:
        out += ["", "<details><summary>Also found (too sad to joke about)</summary>", ""]
        out += [f"- **{f['title']}**: {f['evidence'].splitlines()[0]}" for f in rest]
        out += ["", "</details>"]
    out += ["", f"<sub>Every burn above links to real evidence from commit `{insp.sha[:7]}`. "
                f"Roasted on a Fly.io Sprite.</sub>"]
    return "\n".join(out)


def _parse(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[1] if "\n" in raw else raw
    try:
        return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except ValueError:
        return {}
