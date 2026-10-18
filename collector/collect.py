#!/usr/bin/env python3
"""Collecte quotidienne : sources fixes -> résumé en français (texte public uniquement) -> docs/data/articles.json
Usage :  python collect.py          collecte complète (nécessite ANTHROPIC_API_KEY)
         python collect.py --check  teste seulement chaque flux, sans IA"""
import argparse, calendar, hashlib, html, itertools, json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import feedparser, yaml, urllib

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "docs" / "data" / "articles.json"
MAX_PAR_CAT, CONSERVATION_J, FENETRE_H, SILENCE_J = 12, 7, 72, 5
MODELE = "claude-haiku-5-5"
UA = "veille-perso/1.0 (usage personnel)"

PROMPT = """Tu aides à tenir une veille d'actualité personnelle, en français.
Tu reçois en JSON : la catégorie, le titre et l'EXTRAIT PUBLIC d'un article (le texte complet peut être payant : tu ne l'as pas).
Règles strictes :
- Utilise UNIQUEMENT le titre et l'extrait fournis. Aucune connaissance extérieure, aucun chiffre inventé.
- resume_fr : 2 à 3 phrases factuelles, ton neutre. Si l'extrait est trop pauvre, une seule phrase sur ce qu'annonce le titre et limite=true.
- titre_fr : titre traduit en français, fidèle, sans sensationnalisme.
- pertinent=false si l'article est hors catégorie, publicitaire, une offre d'emploi, un agenda d'événements, ou (santé) un préprint non relu par des pairs.
Réponds uniquement par un objet JSON : {"pertinent": bool, "titre_fr": str, "resume_fr": str, "limite": bool}"""


def nettoie(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def date_entree(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return datetime.fromtimestamp(calendar.timegm(e[k]), timezone.utc)


def lire_source(src, depuis):
    """Retourne (nb_entrées_du_flux, candidats récents)."""
    if src.get("api") == "banque_mondiale":
        return lire_api_bm(src, depuis)
    
    f = feedparser.parse(src["url"], agent=UA)
    if not f.entries:
        return 0, []
    out = []
    for e in f.entries:
        d, lien, titre = date_entree(e), e.get("link"), nettoie(e.get("title"))
        if d and lien and titre and d >= depuis:
            out.append({"titre_original": titre, "lien": lien, "publie": d.isoformat(),
                        "extrait": nettoie(e.get("summary"))[:1500], "source": src["nom"],
                        "type_source": src.get("type", ""), "cat": src["cat"]})
    return len(f.entries), sorted(out, key=lambda a: a["publie"], reverse=True)


def resumer(client, a):
    try:
        r = client.messages.create(
            model=MODELE, max_tokens=500, system=PROMPT,
            messages=[{"role": "user", "content": json.dumps(
                {"categorie": a["cat"], "titre": a["titre_original"], "extrait": a["extrait"]}, ensure_ascii=False)}])
        j = json.loads(re.search(r"\{.*\}", r.content[0].text, re.S).group(0))
        return j if j.get("pertinent") and j.get("resume_fr") else None
    except Exception as ex:
        print(f"  ! résumé impossible ({a['source']}): {ex}", file=sys.stderr)
        return None


def analyse_bm(j, src, depuis):
    """Transforme la réponse JSON de l'API Documents & Reports en candidats."""
    docs = j.get("documents", {})
    out = []
    for d in (docs.values() if isinstance(docs, dict) else docs):
        if not isinstance(d, dict) or not d.get("display_title"):
            continue
        try:
            date = datetime.fromisoformat(d["docdt"].replace("Z", "+00:00"))
        except Exception:
            continue
        lien = d.get("url") or d.get("pdfurl")
        ab = d.get("abstracts")
        extrait = ab.get("cdata!", "") if isinstance(ab, dict) else (ab or "")
        if lien and date >= depuis:
            out.append({"titre_original": d["display_title"].strip(), "lien": lien, "publie": date.isoformat(),
                        "extrait": " ".join(str(extrait).split())[:1500], "source": src["nom"],
                        "type_source": src.get("type", ""), "cat": src["cat"]})
    return len(docs), sorted(out, key=lambda a: a["publie"], reverse=True)


def lire_api_bm(src, depuis):
    params = {"format": "json", "rows": src.get("rows", 50), "srt": "docdt", "order": "desc",
              "fl": "display_title,docdt,docty,url,pdfurl,abstracts",
              "strdate": depuis.date().isoformat(), **src.get("params", {})}
    url = src["url"] + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            j = json.load(r)
    except Exception as ex:
        return 0, [], f"{type(ex).__name__}: {ex}"
    n, cands = analyse_bm(j, src, depuis)
    return n, cands, "API"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    check = ap.parse_args().check
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    now = datetime.now(timezone.utc)
    aujourdhui = now.astimezone(ZoneInfo("Europe/Paris")).date()

    base = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    etat = base.get("etat_sources", {})
    garde = (aujourdhui - timedelta(days=CONSERVATION_J)).isoformat()
    articles = [a for a in base.get("articles", []) if a["lot"] >= garde]
    connus = {a["id"] for a in articles}

    par_cat = {}
    for s in sources:
        n, cands = lire_source(s, now - timedelta(hours=FENETRE_H))
        print(f"{'OK ' if n else 'KO '} {s['nom']:<22} {n:>3} entrées, {len(cands)} récentes")
        if n:
            etat[s["nom"]] = {"derniere_activite": aujourdhui.isoformat()}
        par_cat.setdefault(s["cat"], {}).setdefault(s["nom"], []).extend(cands)
    if check:
        return

    #from anthropic import Anthropic
    #client = Anthropic()
    for cat, par_source in par_cat.items():
        # alternance entre sources pour qu'aucune ne monopolise la catégorie
        melange = [a for tup in itertools.zip_longest(*par_source.values()) for a in tup if a]
        acceptes = appels = 0
        for a in melange:
            a["id"] = hashlib.sha1(a["lien"].encode()).hexdigest()[:12]
            if a["id"] in connus:
                continue
            if acceptes >= MAX_PAR_CAT or appels >= 2 * MAX_PAR_CAT:
                break
            appels += 1
            #j = resumer(client, a)
            #if not j:
            #    continue
            acceptes += 1
            connus.add(a["id"])
            #"categorie": a["cat"], "titre": a["titre_original"], "extrait": a["extrait"]
            articles.append({"id": a["id"], "cat": cat, "titre": a["titre_original"], "resume": a["extrait"][:300],
                             "limite": 0, "lien": a["lien"], "source": a["source"],
                             "type_source": a["type_source"], "publie": a["publie"], "lot": aujourdhui.isoformat()})
        print(f"{cat}: {acceptes} nouveaux articles")

    seuil = (aujourdhui - timedelta(days=SILENCE_J)).isoformat()
    silencieuses = [s["nom"] for s in sources if s["nom"] not in etat or etat[s["nom"]]["derniere_activite"] < seuil]
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps({"maj": now.isoformat(), "lot": aujourdhui.isoformat(), "articles": articles,
                                "etat_sources": etat, "sources_silencieuses": silencieuses},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Écrit : {len(articles)} articles, {len(silencieuses)} source(s) silencieuse(s)")


if __name__ == "__main__":
    main()
