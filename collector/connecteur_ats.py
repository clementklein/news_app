"""Connecteur « offres d'emploi » pour les plateformes de recrutement qui publient des données publiques
(JSON ou XML) : Greenhouse, Lever, Recruitee, Workable, Ashby, SmartRecruiters, Personio.
Utilisé par collect.py via lire_ats(src, depuis) -> (nb_offres, candidats, diagnostic)."""
import html, json, re, unicodedata, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; veille-perso/1.0; usage personnel)"
FENETRE_DEFAUT_J = 7   # doit rester <= à CONSERVATION_J de collect.py, sinon des offres réapparaissent après la purge


def _texte(t):
    t = html.unescape(t or "")                      # Greenhouse renvoie du HTML « échappé »
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def _sans_accent(t):
    return "".join(c for c in unicodedata.normalize("NFD", (t or "").lower()) if unicodedata.category(c) != "Mn")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/xml"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _date(v):
    """ISO 8601, date simple ou millisecondes depuis 1970 -> datetime UTC (None si illisible)."""
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, timezone.utc)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# Chaque lecteur renvoie une liste de dict : titre, lien, date, lieu, equipe, desc
def _greenhouse(c, src):
    j = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{c}/jobs?content=true"))
    return [{"titre": o.get("title"), "lien": o.get("absolute_url"),
             "date": _date(o.get("first_published") or o.get("updated_at")),
             "lieu": (o.get("location") or {}).get("name"), "equipe": None, "desc": o.get("content")}
            for o in j.get("jobs", [])]


def _lever(c, src):
    hote = "api.eu.lever.co" if src.get("region") == "eu" else "api.lever.co"
    j = json.loads(_get(f"https://{hote}/v0/postings/{c}?mode=json"))
    return [{"titre": o.get("text"), "lien": o.get("hostedUrl"), "date": _date(o.get("createdAt")),
             "lieu": (o.get("categories") or {}).get("location"), "equipe": (o.get("categories") or {}).get("team"),
             "desc": o.get("descriptionPlain")} for o in j]


def _recruitee(c, src):
    j = json.loads(_get(f"https://{c}.recruitee.com/api/offers/"))
    return [{"titre": o.get("title"), "lien": o.get("careers_url"), "date": _date(o.get("published_at") or o.get("created_at")),
             "lieu": o.get("location"), "equipe": o.get("department"), "desc": o.get("description")}
            for o in j.get("offers", [])]


def _workable(c, src):
    j = json.loads(_get(f"https://apply.workable.com/api/v1/widget/accounts/{c}?details=true"))
    return [{"titre": o.get("title"), "lien": o.get("url") or f"https://apply.workable.com/{c}/j/{o.get('shortcode')}/",
             "date": _date(o.get("published_on") or o.get("created_at")),
             "lieu": ", ".join(x for x in [o.get("city"), o.get("country")] if x), "equipe": o.get("department"),
             "desc": o.get("description")} for o in j.get("jobs", [])]


def _ashby(c, src):
    j = json.loads(_get(f"https://api.ashbyhq.com/posting-api/job-board/{c}"))
    return [{"titre": o.get("title"), "lien": o.get("jobUrl"), "date": _date(o.get("publishedAt")),
             "lieu": o.get("location"), "equipe": o.get("department"), "desc": o.get("descriptionPlain")}
            for o in j.get("jobs", [])]


def _smartrecruiters(c, src):
    j = json.loads(_get(f"https://api.smartrecruiters.com/v1/companies/{c}/postings?limit=100"))
    return [{"titre": o.get("name"), "lien": f"https://jobs.smartrecruiters.com/{c}/{o.get('id')}",
             "date": _date(o.get("releasedDate")),
             "lieu": ", ".join(x for x in [(o.get("location") or {}).get("city"), (o.get("location") or {}).get("country")] if x),
             "equipe": (o.get("department") or {}).get("label"), "desc": ""} for o in j.get("content", [])]


def _personio(c, src):
    domaine = src.get("domaine", "jobs.personio.de")
    racine = ET.fromstring(_get(f"https://{c}.{domaine}/xml"))
    out = []
    for p in racine.iter("position"):
        pid = p.findtext("id")
        out.append({"titre": p.findtext("name"), "lien": f"https://{c}.{domaine}/job/{pid}", "date": _date(p.findtext("createdAt")),
                    "lieu": p.findtext("office"), "equipe": p.findtext("department"),
                    "desc": " ".join(v.text or "" for v in p.iter("value"))})
    return out


LECTEURS = {"greenhouse": _greenhouse, "lever": _lever, "recruitee": _recruitee, "workable": _workable,
            "ashby": _ashby, "smartrecruiters": _smartrecruiters, "personio": _personio}


def lire_ats(src, _depuis=None):
    """src : nom, cat, ats, compte, [mots_cles], [lieux], [jours], [region], [domaine]."""
    if src.get("ats") not in LECTEURS:
        return 0, [], f"ats inconnu : {src.get('ats')} (choix : {', '.join(LECTEURS)})"
    try:
        offres = LECTEURS[src["ats"]](src["compte"], src)
    except Exception as ex:                          # 404 = nom de compte inexistant, par exemple
        return 0, [], f"{type(ex).__name__}: {ex}"
    maintenant = datetime.now(timezone.utc)
    depuis = maintenant - timedelta(days=src.get("jours", FENETRE_DEFAUT_J))
    mots = [_sans_accent(m) for m in src.get("mots_cles", [])]
    lieux = [_sans_accent(m) for m in src.get("lieux", [])]
    out = []
    for o in offres:
        titre, lien = _texte(o.get("titre")), o.get("lien")
        if not (titre and lien):
            continue
        d = o.get("date") or maintenant
        if d < depuis:
            continue
        if mots and not any(m in _sans_accent(f"{titre} {o.get('equipe') or ''}") for m in mots):
            continue
        if lieux and not any(m in _sans_accent(o.get("lieu")) for m in lieux):
            continue
        entete = " · ".join(x for x in [o.get("lieu"), o.get("equipe")] if x)
        out.append({"titre_original": titre, "lien": lien, "publie": d.isoformat(),
                    "extrait": (entete + ". " if entete else "") + _texte(o.get("desc"))[:600],
                    "source": src["nom"], "type_source": src.get("type", "entreprise"), "cat": src["cat"]})
    # une liste vide n'est pas une panne : on renvoie au moins 1 pour que la source reste « active »
    return max(len(offres), 1), sorted(out, key=lambda a: a["publie"], reverse=True), f"{len(offres)} offre(s) au total"
