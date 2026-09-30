#!/usr/bin/env python3
"""Hourly EE internship scanner. Polls company career sites / ATS APIs directly, emails new US matches."""
import collections, datetime, html, json, os, re, smtplib, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).parent
SEEN, BOARDS, COMPANIES, UNSCANNABLE = HERE / "seen.json", HERE / "boards.json", HERE / "companies.txt", HERE / "unscannable.txt"
NAMES = HERE / "names.json"  # board -> company display name
POSTED = {}  # url -> when Simplify listed it (for roles we only see via Simplify)
TO = os.environ.get("JOBSCAN_TO")                      # where alerts go (kept out of the public repo)
FROM = os.environ.get("JOBSCAN_FROM") or TO            # the sending Gmail account
PACIFIC = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
DISCOVERY = [  # company directory only: harvests which boards to poll (+ roles on sites we can't poll)
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"

# ---------- filters ----------
INTERN = re.compile(r"\bintern(ship)?s?\b", re.I)
COOP = re.compile(r"\bco-?op\b", re.I)
EE = re.compile(r"electrical|electronic|hardware|\bpcb|layout|analog|mixed[- ]signal|power|\brf\b|microwave|antenna|"
    r"signal integrity|\bsi\b|\bpi\b|\brtl\b|asic|\bsoc\b|digital design|verification|\bdv\b|physical design|silicon|"
    r"circuit|\bic\b|validation|\btest|process|yield|semiconductor|fpga|embedded|firmware|systems eng|controls?\b|"
    r"\bhil\b|simulation|robotic|mechatronic|avionic|harness|ewis|integration|\bgnc\b|guidance|navigation|battery|"
    r"energy|\bemi\b|\bemc\b|propulsion|powertrain|motor|reliability|manufactur|quality|compliance|certification|"
    r"assembly|automation|component|supplier|substation|protection|\bp&c\b|transmission|distribution|\bt&d\b|grid|"
    r"interconnection|utility|renewable|solar|wind|storage|nuclear|generation|data ?center|critical|\bmep\b|lighting|"
    r"field eng|construction|project eng|estimat|low[- ]voltage|telecom|\bdsp\b|signal processing|communications|"
    r"wireless|network hardware|optical|photonic|laser|applications eng|\bfae\b|technical sales|program manag|"
    r"research (?:intern|eng)|engineering research|instrumentation|\bi&c\b|medical device|technician|\blab\b|sensor|"
    r"aerospace|space|satellite", re.I)
NOT_EE = re.compile(r"marketing|finance|\bbusiness\b|devops|\btax\b|mrna|biolog|pharma|clinical|supply chain|customer|"
    r"support engineer|security engineer|cyber|penetration|human computer|\bhci\b|human factors|traffic|accounting|human resources|\bhr\b|recruit|legal|\bux\b|graphic|"
    r"analyst|data scien|civil|agronom|web|quant|site reliability|user experience|game|corporate|"
    r"frontend|backend|full[- ]?stack|product design|communications? (?:intern|specialist|manager)", re.I)

STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
    "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
    "Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
    "Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|"
    "Washington|West Virginia|Wisconsin|Wyoming|District of Columbia")
ABBR = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
    "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
US = re.compile(rf"United States|\bUSA?\b|U\.S\.|\b(?:{STATES})\b|(?:^|[,\s-])(?:{ABBR})(?=$|[,\s\d-])")
US_EXPLICIT = re.compile(r"United States|\bUSA?\b|U\.S\.")
FOREIGN = re.compile(r"Canada|Ontario|Quebec|British Columbia|Alberta|Toronto|Vancouver|Montr[eé]al|Waterloo|Mexico|"
    r"India|China|Taiwan|Japan|Korea|Singapore|Malaysia|Philippines|Vietnam|Thailand|Indonesia|Israel|Germany|France|"
    r"United Kingdom|\bUK\b|England|Scotland|Ireland|Netherlands|Poland|Spain|Italy|Switzerland|Sweden|Denmark|Norway|"
    r"Finland|Belgium|Austria|Czech|Romania|Hungary|Portugal|Brazil|Argentina|Chile|Colombia|Costa Rica|Australia|"
    r"New Zealand|Egypt|South Africa|Emirates|Dubai|Saudi|Turkey|Hsinchu|Bangalore|Bengaluru|Hyderabad|Shanghai|"
    r"Beijing|Shenzhen|Tokyo|Seoul|Penang|London|Munich|Paris|Dublin|Krak[oó]w|Berlin", re.I)
AMBIGUOUS = re.compile(r"^[\s,;]*$|\d+ Locations|Multiple|Various|Flexible|\bAny\b|Nationwide|TBD|^Remote$|Hybrid$", re.I)
SEATTLE = (r"Seattle|Redmond|Bellevue|Kirkland|Bothell|Everett|Renton|Tacoma|Kent, W|Tukwila|Issaquah|Woodinville|"
    r"Lynnwood|Mukilteo|Puget Sound|Auburn, W")
SEATTLE_RE = re.compile(SEATTLE, re.I)
WA = re.compile(r"\bWA\b|Washington(?! D\.?C)", re.I)


def seattle(loc):
    """Seattle-area city *in Washington State* (not Bellevue NE/IA) in any ;-separated location."""
    return any(SEATTLE_RE.search(p) and (WA.search(p) or re.search(r"Seattle|Puget", p, re.I)) for p in loc.split(";"))


PREFERRED = re.compile(r"San Francisco|Bay Area|San Jose|Santa Clara|Sunnyvale|Mountain View|Palo Alto|"
    r"Menlo Park|Cupertino|Oakland|Fremont|Milpitas|Redwood|San Mateo|Berkeley|Hayward|Pleasanton|Livermore|"
    r"San Carlos|Foster City|Austin|Houston|Dallas|Fort Worth|Plano|Richardson|Irving|Frisco|Chicago|Pittsburgh|"
    r"New York|NYC|Brooklyn|Manhattan|Orlando|Miami|Los Angeles|El Segundo|Hawthorne|Torrance|Long Beach|Irvine|"
    r"Pasadena|Santa Monica|Culver City|Burbank|San Diego|California|\bCA\b|Oregon|Portland|Hillsboro|Beaverton|"
    r"(?:^|[,\s-])OR(?=$|[,\s\d-])|Detroit|Ann Arbor|Dearborn|Auburn Hills|Warren, MI|Troy, MI|Novi|Livonia|"
    r"Southfield|Pontiac|Indianapolis|Carmel, IN", re.I)


OTHER_DISCIPLINE = re.compile(r"mechanical|chemical|industrial|environmental|biomedical|structural|geotech|petroleum|"
    r"materials|process|business|operations|supply chain|sourcing|finance", re.I)


GRAD = re.compile(r"ph\.?d|doctoral|post-?doc|master'?s|\bmba\b|\bms\b|\bm\.s\.|(?<!under)graduate|residency", re.I)
UNDERGRAD = re.compile(r"undergrad|bachelor|\bbs\b|\bb\.s\.", re.I)
NOT_HW = re.compile(r"software|\bai\b|artificial intelligence|machine learning|\bml\b|deep learning|data (?:engineer|platform)|"
    r"applied scien|research scien|\bdata (?!cent)|recommendation|agentic|python|\bllm|\bnlp\b", re.I)
HW = re.compile(r"embedded|firmware|hardware|electrical|fpga|asic|silicon|circuit", re.I)


def tier(loc): return 0 if seattle(loc) else 1 if PREFERRED.search(loc) else 2  # Seattle > preferred > rest


def match(t):
    if GRAD.search(t) and not UNDERGRAD.search(t): return False  # undergrad roles only
    sw = NOT_HW.search(t) and not HW.search(t)
    ee = EE.search(t) or (re.search(r"engineer", t, re.I) and not OTHER_DISCIPLINE.search(t))  # generic "Engineering Internship"
    return bool((INTERN.search(t) or COOP.search(t)) and ee and not NOT_EE.search(t) and not sw)


def us(loc):
    """Any ;-separated location in the US -> True."""
    for part in loc.split(";"):
        if AMBIGUOUS.search(part): return True  # "3 Locations" etc: can't tell, don't drop
        if FOREIGN.search(part) and not US_EXPLICIT.search(part): continue
        if re.search(r", [A-Z]{2}, (?!US$)[A-Z]{2}\s*$", part): continue  # "Markham, ON, CA" / "Cork, CO, IE"
        if US.search(part): return True
    return False


def keep(title, loc):
    """Internships: anywhere in US. Co-ops (not also internships): Seattle area only."""
    if not (match(title) and us(loc)): return False
    return bool(INTERN.search(title) or seattle(loc))


# ---------- fetching ----------
def fetch(url, data=None, headers=None, raw=False, timeout=30, tries=4):
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": UA,
                                             "Accept-Language": "en-US", **(headers or {})})
    for attempt in range(tries):  # retry rate limits / flaky servers; a 404 (dead board) fails fast
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                s = r.read().decode("utf-8", "ignore")
            return s if raw else json.loads(s)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1: raise
            time.sleep(min(int(e.headers.get("Retry-After") or 0) or 5 * 2 ** attempt, 60))
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1: raise
            time.sleep(5 * 2 ** attempt)


QUERIES = ("intern", "co-op")


def s_gh(tok):
    return [(j["absolute_url"], tok, j["title"], j["location"]["name"])
            for j in fetch(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs", timeout=120)["jobs"]]  # some boards are MBs


def s_lever(tok, api="api.lever.co"):
    return [(j["hostedUrl"], tok, j["text"], j["categories"].get("location") or "")
            for j in fetch(f"https://{api}/v0/postings/{tok}?mode=json")]


def s_workable(tok):
    out, body = [], {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    for _ in range(20):
        d = fetch(f"https://apply.workable.com/api/v3/accounts/{tok}/jobs", body)
        out += [(f"https://apply.workable.com/{tok}/j/{j['shortcode']}/", tok, j["title"],
                 ", ".join(filter(None, (j["location"].get(k) for k in ("city", "region", "country"))))) for j in d["results"]]
        if not d.get("nextPage"): break
        body = {**body, "token": d["nextPage"]}
    return out


def s_bamboo(tok):
    return [(f"https://{tok}.bamboohr.com/careers/{j['id']}", tok, j["jobOpeningName"],
             ", ".join(filter(None, (j["location"].get("city"), j["location"].get("state"), (j.get("atsLocation") or {}).get("country")))))
            for j in fetch(f"https://{tok}.bamboohr.com/careers/list")["result"]]


def s_rippling(tok):
    return [(j["url"], tok, j["name"], "; ".join(f"{l.get('name')}, {l.get('country')}" for l in j["locations"]))
            for j in fetch(f"https://ats.rippling.com/api/v2/board/{tok}/jobs?page=0&pageSize=200")["items"]]


def s_bytedance(api, path, job_url):
    """TikTok / ByteDance share one job API."""
    out = []
    for q in QUERIES:
        for off in range(0, 500, 100):
            js = fetch(api, {"keyword": q, "limit": 100, "offset": off, "recruitment_id_list": [], "job_category_id_list": [],
                             "subject_id_list": [], "location_code_list": []}, headers={"website-path": path})["data"]["job_post_list"]
            for j in js:
                c, loc = j.get("city_info"), []
                while c: loc.append(c.get("en_name") or ""); c = c.get("parent")
                out.append((job_url + j["id"], path.replace("en", "bytedance"), j["title"], ", ".join(loc)))
            if len(js) < 100: break
    return out


def s_ashby(tok):
    return [(j["jobUrl"], tok, j["title"], j.get("location", ""))
            for j in fetch(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")["jobs"]]


def s_sr(tok):
    return [(f"https://jobs.smartrecruiters.com/{tok}/{j['id']}", j["company"]["name"], j["name"],
             j["location"].get("fullLocation") or f"{j['location'].get('city')}, {j['location'].get('country')}")
            for q in QUERIES for j in fetch(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?q={q}")["content"]]


def s_wd(host, tenant, site):
    out = []
    prefix = f"https://{host}/recruiting/{tenant}/{site}" if "myworkdaysite" in host else f"https://{host}/{site}"
    for q, pages in (("intern", 5), ("co-op", 1)):  # ponytail: 100 relevance-ranked hits per tenant; raise if a giant tenant misses roles
        for off in range(0, 20 * pages, 20):
            page = fetch(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                         {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q}).get("jobPostings", [])
            out += [(prefix + j["externalPath"], tenant, j["title"], j.get("locationsText", "")) for j in page if "externalPath" in j and "title" in j]
            if len(page) < 20: break
    return out


def s_oracle(host, site):
    out = []
    for q in QUERIES:
        for off in range(0, 100, 25):
            it = fetch(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                       f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},keyword={q},"
                       f"limit=25,offset={off},sortBy=POSTING_DATES_DESC")["items"][0]
            reqs = it.get("requisitionList", [])
            out += [(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}", host.split(".")[0],
                     r["Title"], f"{r.get('PrimaryLocation', '')}, {r.get('PrimaryLocationCountry', '')}") for r in reqs]
            if len(reqs) < 25: break
    return out


def s_icims(host):
    out = []
    for q in QUERIES:
        s = fetch(f"https://{host}/jobs/search?ss=1&searchKeyword={q}&in_iframe=1", raw=True)
        for row in s.split('class="col-xs-12 title"')[1:]:
            u = re.search(r'href="([^"]+)"', row); t = re.search(r"<h3[^>]*>\s*([^<]+)", row)
            loc = re.search(r"Job Location.*?<span[^>]*>\s*([^<]+)", row, re.S)
            if u and t: out.append((u.group(1).replace("?in_iframe=1", ""), host.split(".")[0].split("-", 1)[-1],
                                    html.unescape(t.group(1).strip()), loc.group(1).strip() if loc else ""))
    return out


def s_jibe(host):
    out = []
    for q in QUERIES:
        for p in range(1, 11):
            jobs = fetch(f"https://{host}/api/jobs?keywords={q}&page={p}")["jobs"]
            out += [(f"https://{host}/jobs/{d['slug']}", host, d["title"],
                     f"{d.get('city', '')}, {d.get('state', '')}, {d.get('country', '')}") for d in (j["data"] for j in jobs)]
            if len(jobs) < 10: break
    return out


def s_sfrmk(host):
    """SuccessFactors Recruiting Marketing sites (…/job/City-Title-ST-12345/123/): HTML search pages."""
    out = {}
    for q in QUERIES:
        for row in range(0, 200, 25):
            s = fetch(f"https://{host}/search/?q={q}&startrow={row}", raw=True)
            before = len(out)
            for m in re.finditer(r'href="(/job/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>', s):
                loc = re.search(r'(?:section-location-value"|class="jobLocation[^"]*")>\s*([^<]+?)\s*<', s[m.end():m.end() + 4000])
                out.setdefault(m.group(1), (f"https://{host}{m.group(1)}", host, html.unescape(m.group(2)), loc.group(1) if loc else ""))
            if len(out) == before: break
    return list(out.values())


def s_phenom(base):
    host, *rest = base.split("/")
    country, lang = (rest + ["global", "en"])[:2] if len(rest) == 2 else ("global", rest[0] if rest else "en")
    out = []
    for q in QUERIES:
        for frm in range(0, 200, 50):
            jobs = fetch(f"https://{host}/widgets", {
                "lang": f"{lang}_{country}", "deviceType": "desktop", "country": country, "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": frm, "jobs": True, "counts": False,
                "all_fields": [], "size": 50, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page1", "siteType": "external", "keywords": q, "global": True, "selected_fields": {},
                "locationData": {}})["refineSearch"]["data"]["jobs"]
            out += [(f"https://{base}/job/{j['jobId']}", host, j["title"],
                     j.get("cityStateCountry") or j.get("location", "")) for j in jobs]
            if len(jobs) < 50: break
    return out


def s_ef(host, domain):
    out = []
    for q in QUERIES:
        for start in range(0, 100, 10):
            ps = fetch(f"https://{host}/api/pcsx/search?domain={domain}&query={q}&start={start}",
                       headers={"Accept": "application/json"})["data"]["positions"]
            out += [(f"https://{host}{p['positionUrl']}", domain, p["name"], "; ".join(p.get("standardizedLocations") or p["locations"]))
                    for p in ps]
            if len(ps) < 10: break
    return out


def s_taleo(host, section):
    page = fetch(f"https://{host}/careersection/{section}/jobsearch.ftl", raw=True)
    portal = re.search(r"portal=(\d+)", page)
    if not portal: return []
    out = []
    for q in QUERIES:
        for n in range(1, 5):
            reqs = fetch(f"https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal.group(1)}",
                         {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                          "fieldData": {"fields": {"KEYWORD": q, "LOCATION": ""}, "valid": True},
                          "filterSelectionParam": {"searchFilterSelections": []},
                          "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []}, "pageNo": n},
                         headers={"tz": "GMT-07:00"}).get("requisitionList", [])
            out += [(f"https://{host}/careersection/{section}/jobdetail.ftl?job={r['contestNo']}", host.split(".")[0],
                     r["column"][0], " ".join(json.loads(r["column"][1])) if r["column"][1].startswith("[") else r["column"][1])
                    for r in reqs]
            if len(reqs) < 25: break
    return out


def s_apple():
    out = []
    for p in range(1, 6):  # Apple's "Internships" team; keyword search is full-text and useless
        s = fetch(f"https://jobs.apple.com/en-us/search?location=united-states-USA&team=internships-STDNT-INTRN&page={p}", raw=True)
        m = re.search(r'__staticRouterHydrationData = JSON.parse\((".*?")\);', s, re.S)
        res = json.loads(json.loads(m.group(1)))["loaderData"]["search"]["searchResults"] if m else []
        out += [(f"https://jobs.apple.com/en-us/details/{r['positionId']}", "Apple", r["postingTitle"],
                 "; ".join(f"{l.get('name')}, {l.get('countryName')}" for l in r["locations"])) for r in res]
        if len(res) < 20: break
    return out


def s_google():
    out = []
    for q in QUERIES:
        for p in range(1, 6):
            s = fetch(f"https://www.google.com/about/careers/applications/jobs/results?q={q}&location=United%20States&page={p}", raw=True)
            m = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(\[.*?\]), sideChannel", s, re.S)
            jobs = (json.loads(m.group(1))[0] or []) if m else []
            out += [(f"https://www.google.com/about/careers/applications/jobs/results/{j[0]}", "Google", j[1],
                     "; ".join(l[0] for l in j[9] or [])) for j in jobs]
            if len(jobs) < 20: break
    return out


def s_amazon():
    out = []
    for q in QUERIES:
        for off in range(0, 500, 100):
            jobs = fetch(f"https://www.amazon.jobs/en/search.json?base_query={q}&country=USA&result_limit=100&offset={off}&sort=recent")["jobs"]
            out += [("https://www.amazon.jobs" + j["job_path"], "Amazon", j["title"], j["normalized_location"] or j["location"]) for j in jobs]
            if len(jobs) < 100: break
    return out


SCANNERS = {"gh": s_gh, "lever": s_lever, "ashby": s_ashby, "sr": s_sr, "wd": s_wd, "oracle": s_oracle, "icims": s_icims,
            "jibe": s_jibe, "sfrmk": s_sfrmk, "phenom": s_phenom, "ef": s_ef, "taleo": s_taleo,
            "apple": s_apple, "google": s_google, "amazon": s_amazon,
            "workable": s_workable, "bamboo": s_bamboo, "rippling": s_rippling, "bytedance": s_bytedance}
MSFT = ("ef", "apply.careers.microsoft.com", "microsoft.com")
TIKTOK = ("bytedance", "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts", "tiktok", "https://lifeattiktok.com/search/")
BYTEDANCE = ("bytedance", "https://jobs.bytedance.com/api/v1/search/job/posts", "en", "https://jobs.bytedance.com/en/position/")
BUILTIN = {("apple",), ("google",), ("amazon",), MSFT, TIKTOK, BYTEDANCE}


def detect(url):
    """Career/job URL -> board tuple, or None if we can't poll that site."""
    u = urllib.parse.unquote(url)
    for dom, b in (("jobs.apple.com", ("apple",)), ("google.com/about/careers", ("google",)), ("amazon.jobs", ("amazon",)),
                   ("careers.microsoft.com", MSFT), ("lifeattiktok.com", TIKTOK), ("jobs.bytedance.com", BYTEDANCE)):
        if dom in u: return b
    rules = [
        ("wd", r"https?://(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[a-z]{2}/)?recruiting/([\w-]+)/([\w-]+)", lambda h, t, s: (h, t, s)),
        ("wd", r"https?://(([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", lambda h, t, s: (h, t, s)),
        ("gh", r"greenhouse\.io/(?:embed/job_app\?for=|v1/boards/)?([\w-]+)", lambda t: (t,)),
        ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)", lambda t: (t,)),
        ("lever", r"jobs\.eu\.lever\.co/([^/?#]+)", lambda t: (t, "api.eu.lever.co")),
        ("lever", r"jobs\.lever\.co/([^/?#]+)", lambda t: (t,)),
        ("workable", r"apply\.workable\.com/(?!api/)([\w-]+)", lambda t: (t,)),
        ("bamboo", r"https?://([\w-]+)\.bamboohr\.com", lambda t: (t,)),
        ("rippling", r"ats\.rippling\.com/(?!api/)([\w-]+)", lambda t: (t,)),
        ("sr", r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", lambda t: (t,)),
        ("oracle", r"https?://([\w.-]+\.oraclecloud\.com)(?::443)?/hcmUI/CandidateExperience/[a-z]{2}/sites/([\w-]+)", lambda h, s: (h, s)),
        ("jibe", r"https?://([\w.-]+)/jobs/\d+\?(?:lang=[\w-]+&)?icims=1", lambda h: (h,)),
        ("jibe", r"https?://([\w.-]+)/?\?jibe$", lambda h: (h,)),
        ("icims", r"https?://((?!internal-)[\w-]+\.icims\.com)/jobs", lambda h: (h,)),
        ("taleo", r"https?://([\w-]+\.taleo\.net)/careersection/([\w-]+)/", lambda h, s: (h, s)),
        ("ef", r"https?://([\w-]+)\.eightfold\.ai", lambda s: (f"{s}.eightfold.ai", f"{s}.com")),
        ("ef", r"https?://([\w.-]+)/careers\?.*\bdomain=([\w.-]+)", lambda h, d: (h, d)),
        ("sfrmk", r"https?://([\w.-]+)/(?:job|search)/.*[?&]ats=successfactors", lambda h: (h,)),
        ("sfrmk", r"https?://([\w.-]+)/?\?sfrmk$", lambda h: (h,)),
        ("phenom", r"https?://([\w.-]+/(?:[a-z]{2,6}/)?[a-z]{2})/job/", lambda b: (b,)),
        ("phenom", r"https?://([\w.-]+/(?:[a-z]{2,6}/)?[a-z]{2})/?\?phenom$", lambda b: (b,)),
    ]
    for kind, pat, f in rules:
        if m := re.search(pat, u, re.I):
            b = (kind, *f(*m.groups()))
            return None if kind == "wd" and b[-1].lower() in ("job", "wday", "login") else b
    return None


def gh_custom_domain(j):
    """Greenhouse board behind a company's own domain (…?gh_jid=123): find its board token so we poll it directly."""
    if "gh_jid=" not in j.get("url", "") or not j.get("active") or not keep(j["title"], "; ".join(j.get("locations", []))):
        return None
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    label = urllib.parse.urlparse(j["url"]).netloc.split(".")[-2]
    for tok in dict.fromkeys([norm(j["company_name"]), label, "fly" + label, label.removeprefix("with")]):
        try:
            if norm(fetch(f"https://boards-api.greenhouse.io/v1/boards/{tok}")["name"])[:5] == norm(j["company_name"])[:5]:
                return ("gh", tok)
        except Exception: pass
    return None


def discover():
    """Boards to poll = saved boards ∪ companies.txt ∪ boards harvested from public listings. Only grows."""
    boards = set(map(tuple, json.loads(BOARDS.read_text()))) if BOARDS.exists() else set()
    boards |= BUILTIN
    names = json.loads(NAMES.read_text()) if NAMES.exists() else {}
    names.update({"|".join(b): n for b, n in ((("apple",), "Apple"), (("google",), "Google"), (("amazon",), "Amazon"),
                                              (MSFT, "Microsoft"), (TIKTOK, "TikTok"), (BYTEDANCE, "ByteDance"))})
    if COMPANIES.exists():
        for raw in COMPANIES.read_text().splitlines():
            line, _, comment = raw.partition("#")
            if (line := line.strip()) and (b := detect(line)):
                boards.add(b)
                if comment.strip(): names["|".join(b)] = comment.strip()
            elif line: print("companies.txt: can't poll", line, file=sys.stderr)
    fallback, blind = [], collections.defaultdict(list)
    for src in DISCOVERY:
        try: listings = fetch(src)
        except Exception as e: print("discovery failed", src, e, file=sys.stderr); continue
        for j in listings:
            if b := detect(j.get("url", "")) or gh_custom_domain(j):
                boards.add(b)
                names.setdefault("|".join(b), j["company_name"])
            elif j.get("active"):  # site we can't poll (Tesla, Meta, ...): use the listing itself, with Simplify's post time
                POSTED[j["url"]] = datetime.datetime.fromtimestamp(j["date_posted"], datetime.timezone.utc)
                fallback.append((j["url"], f"{j['company_name']} (via Simplify)", j["title"], "; ".join(j.get("locations", []))))
                if keep(j["title"], fallback[-1][3]):
                    blind[f"{j['company_name']} ({urllib.parse.urlparse(j['url']).netloc})"].append(j["date_posted"])
    UNSCANNABLE.write_text(
        "Companies with open EE internships whose own career site can't be polled; their roles come from Simplify.\n"
        "EE roles | newest listed on Simplify | company (site)\n" + "".join(
            f"{len(t):8} | {datetime.datetime.fromtimestamp(max(t)):%b %d %I:%M %p}        | {c}\n"
            for c, t in sorted(blind.items(), key=lambda kv: -len(kv[1]))))
    boards = set({tuple(x.lower() for x in b): b for b in sorted(boards)}.values())  # "External" == "external"
    BOARDS.write_text(json.dumps(sorted(boards), indent=0))
    NAMES.write_text(json.dumps(names, indent=0, sort_keys=True))
    return boards, fallback


def scan(b):
    try: return SCANNERS[b[0]](*b[1:])
    except Exception as e: return e  # dead/renamed board or site hiccup; next hour retries


# ---------- email ----------
def secret():
    return os.environ.get("JOBSCAN_PASS") or subprocess.run(
        ["security", "find-generic-password", "-s", "jobscan", "-w"], capture_output=True, text=True).stdout.strip()


NUM = r"\d{1,3}(?:,?\d{3})*(?:\.\d{2})?"
PAY = re.compile(rf"(?:\$|USD ?){NUM}(?: ?USD)?[kK]? ?(?:-|–|—|to) ?(?:\$|USD ?)?{NUM}(?: ?USD)?[kK]?(?: ?(?:/|per) ?(?:hour|hr|year|yr|annum|month))?|"
                 rf"{NUM} ?USD ?(?:-|–|to) ?{NUM} ?USD(?: ?(?:/|per) ?(?:hour|hr|year|yr))?|\$\d{{2,3}}(?:\.\d{{2}})? ?(?:/|per) ?(?:hour|hr)", re.I)


def details(url):
    """-> (posted datetime|None, pay str|None, company str|None) from the job's own page/API. Best effort."""
    try:
        if m := re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url):
            d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}?pay_transparency=true", timeout=15, tries=1)
            rng = (d.get("pay_input_ranges") or [{}])[0]
            pay = rng and rng.get("min_cents") and f"${rng['min_cents'] / 100:,.0f} – ${rng['max_cents'] / 100:,.0f}"
            return (datetime.datetime.fromisoformat(d.get("first_published") or d["updated_at"]),
                    pay or (PAY.search(html.unescape(re.sub(r"<[^>]+>|&lt;[^&]*&gt;", " ", html.unescape(d.get("content", ""))))) or [None])[0],
                    d.get("company_name"))
        if m := re.search(r"https://([\w.-]+myworkday(?:jobs|site)\.com)/(?:recruiting/([\w-]+)/)?([\w-]+)(/job/.*)", url):
            host, tenant, site, path = m.groups()
            tenant = tenant or host.split(".")[0]
            info = fetch(f"https://{host}/wday/cxs/{tenant}/{site}{path}", timeout=15, tries=1)["jobPostingInfo"]
            text = html.unescape(re.sub(r"<[^>]+>", " ", info.get("jobDescription", "")))
            return (datetime.datetime.fromisoformat(info["startDate"]).replace(tzinfo=PACIFIC) if info.get("startDate") else None,
                    (PAY.search(text) or [None])[0], None)
        page = fetch(url + ("&" if "?" in url else "?") + "in_iframe=1" if ".icims.com/" in url else url, raw=True, timeout=15, tries=1)
        posted = pay = org = None
        for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
            try: d = json.loads(block)
            except ValueError: continue
            d = next((x for x in (d if isinstance(d, list) else [d]) if "JobPosting" in str(x.get("@type"))), None)
            if not d: continue
            if d.get("datePosted"):
                posted = datetime.datetime.fromisoformat(d["datePosted"].replace("Z", "+00:00").replace("+0000", "+00:00"))
            org = (d.get("hiringOrganization") or {}).get("name")
            v = ((d.get("baseSalary") or {}).get("value") or {})
            if v.get("minValue"):
                pay = f"${float(v['minValue']):,.2f} – ${float(v.get('maxValue') or v['minValue']):,.2f}".replace(".00", "") + \
                      (f" / {v['unitText'].lower()}" if v.get("unitText") else "")
        text = html.unescape(re.sub(r"<[^>]+>", " ", page))
        return posted, pay or (PAY.search(text) or [None])[0], org
    except Exception:
        return None, None, None


def pretty(c):
    if c.islower() and " " in c: return c.title()  # "trane technologies" -> "Trane Technologies"
    c = re.sub(r"^(?:careers?|jobs|corningjobs)[.-]|\.(?:com|org|net|io|ai)(?:/.*)?$", "", c)
    return c[:1].upper() + c[1:]


def email(jobs, seen_at, priority_names, send=True):
    """HTML alert: Seattle / preferred / other US; posted, company, role, location, pay, Apply button."""
    esc = html.escape
    with ThreadPoolExecutor(32) as ex:  # look up pay/post date for the new roles only
        ranked = sorted(jobs, key=lambda j: (tier(j[3]), j[1] not in priority_names))
        # ponytail: pay/date lookups capped at 300 per email; only the first-ever run is bigger than that
        info = dict(zip((j[0] for j in ranked[:300]), ex.map(details, (j[0] for j in ranked[:300]))))

    def when(j):
        p = POSTED.get(j[0]) or info.get(j[0], (None,))[0]
        if p and p.tzinfo is None: p = p.replace(tzinfo=datetime.timezone.utc)
        return (p.astimezone(PACIFIC).strftime("%b %-d, %-I:%M %p") if p.hour or p.minute else p.strftime("%b %-d")) if p else \
            "found " + seen_at.astimezone(PACIFIC).strftime("%b %-d, %-I:%M %p")

    def row(j):
        u, c, t, loc = j
        _, pay, org = info.get(u, (None, None, None))
        company = (org if org and len(org) < 40 else None) or c
        loc = loc if len(loc) < 90 else loc[:87] + "…"
        td = 'style="padding:10px 8px;border-bottom:1px solid #e3e7e5;vertical-align:top;font-size:14px"'
        return (f'<tr><td {td}>{esc(when(j))}</td><td {td}><b>{esc(company)}</b></td><td {td}>{esc(t)}</td>'
                f'<td {td}>{esc(loc)}</td><td {td}>{esc(pay or "Not listed")}</td>'
                f'<td {td}><a href="{esc(u)}" style="display:inline-block;background:#1f5a43;color:#fff;text-decoration:none;'
                f'padding:7px 14px;border-radius:5px;font-weight:600;white-space:nowrap">Apply</a></td></tr>')

    th = 'style="text-align:left;padding:8px;font-size:12px;color:#5d6a65;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #1f5a43"'
    head = "".join(f"<th {th}>{h}</th>" for h in ("Posted", "Company", "Role", "Location", "Pay", ""))
    counts = [sum(tier(j[3]) == n for j in jobs) for n in range(3)]
    shown = ranked[:300]  # ponytail: Gmail clips ~100KB emails; only the first-ever run has more new roles than this
    groups = [[j for j in shown if tier(j[3]) == n] for n in range(3)]
    sections = "".join(
        f'<h2 style="font-size:17px;margin:28px 0 8px;color:#18201d">{name} ({len(g)})</h2>'
        f'<table style="border-collapse:collapse;width:100%">{head}{"".join(map(row, g))}</table>'
        for name, g in zip(("★★ Seattle area", "★ Your preferred cities", "Other US"), groups) if g)
    top = list(dict.fromkeys((info.get(j[0], (0, 0, None))[2] or j[1]).split(" (via")[0] for j in ranked))[:4]
    subject = f"New EE Internship Alert: {', '.join(top)} ({len(jobs)} new role{'s' * (len(jobs) > 1)})"
    body = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#18201d;max-width:1000px">'
            f'<p style="font-size:15px">{len(jobs)} new undergrad EE internships: <b>{counts[0]} in the Seattle area</b>, '
            f'{counts[1]} in your other preferred cities.'
            + (f' Showing the top {len(shown)}, Seattle and preferred cities first.' if len(shown) < len(jobs) else '') +
            f' Posted time comes from the company\'s own listing where it gives one; '
            f'otherwise it\'s when the scanner first found the role (it checks every 30 minutes).</p>{sections}</div>')
    (HERE / "last_email.html").write_text(f'<meta charset="utf-8"><!-- Subject: {esc(subject)} -->\n{body}')
    if not send: return print("Subject:", subject)
    msg = MIMEText(body, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, f"EE Internship Alerts <{FROM}>", TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(FROM, secret())
        s.send_message(msg)


def main(dry=False):
    boards, jobs = discover()
    jobs = [j for j in jobs if keep(j[2], j[3])]
    low = lambda b: tuple(x.lower() for x in b)
    priority = {low(b) for b in BUILTIN} | {low(b) for l in (COMPANIES.read_text().splitlines() if COMPANIES.exists() else []) if (b := detect(l.split("#")[0].strip()))}
    failed, names = [], json.loads(NAMES.read_text())
    with ThreadPoolExecutor(64) as ex:
        for b, res in zip(boards, ex.map(scan, boards)):
            if isinstance(res, Exception):
                if low(b) in priority: failed.append(f"{b[1]}: {res!r:.80}")
                continue
            name = pretty(names.get("|".join(b)) or (b[1] if len(b) > 1 else b[0]))
            jobs += [(u, name, t, l) for u, c, t, l in res if keep(t, l)]
    jobs = list({j[0]: j for j in jobs}.values())
    for f in failed: print("PRIORITY BOARD FAILED", f, file=sys.stderr)
    if dry:
        for u, c, t, l in sorted(jobs, key=lambda j: tier(j[3])):
            print(["★★", "★ ", "  "][tier(l)] + f" {c} | {t} | {l}\n    {u}")
        return print(f"{len(jobs)} matches")
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}  # url -> when we first saw it (≈ when it dropped)
    new = [j for j in jobs if j[0] not in seen]
    print(f"{len(boards)} boards, {len(jobs)} matches, {len(new)} new", flush=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    if new:
        email(new, now, {names.get("|".join(b)) for b in boards if low(b) in priority})  # send first: a failed send retries next run
        SEEN.write_text(json.dumps(seen | {j[0]: now.isoformat(timespec="minutes") for j in new}, indent=0))
    if len(failed) > len(priority) // 4 or not jobs:  # make the run fail loudly (GitHub emails you) instead of quietly missing roles
        sys.exit(f"{len(failed)} of {len(priority)} priority boards failed; {len(jobs)} matches")


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        assert match("PCB Design Intern") and match("Power Systems Engineering Intern - Summer 2027")
        assert match("Design Verification Co-op") and match("FPGA Engineering Intern")
        assert not match("Senior Electrical Engineer") and not match("Marketing Intern") and match("Technical Sales Intern")
        assert not match("Software Engineer Intern") and match("Embedded Software Engineer Intern")
        assert keep("Summer 2027 Engineering Internship/Co-op", "Flexible - Any SpaceX Site")
        assert match("Spring 2027 Engineering Internship/Co-op") and not match("Spring 2027 Business Operations Internship/Co-op")
        assert not match("Hardware Masters Engineering Internships") and match("Hardware Undergrad Engineering Internships")
        assert not match("Silicon Engineering Intern, PhD, Summer 2027") and match("Silicon Engineering Intern, BS/MS, Summer 2027")
        assert not match("Spring 2027 Graduate Engineer Internship/Co-op") and match("Undergraduate Electrical Intern")
        assert not match("AI Engineering Intern") and not match("Data Engineering Intern") and match("AI Hardware Engineering Intern")
        assert tier("bellevue, Iowa, United States") == 2 and tier("United States-Nebraska-Bellevue") == 2
        assert tier("USA-WA-Seattle") == 0 and tier("Bothell, Washington, United States") == 0 and tier("Kent, WA") == 0
        assert not match("Income Tax Compliance Internship") and not match("Research Intern - Data Systems")
        assert match("Data Center Critical Power Intern") and not match("Supply Chain Program Management Intern")
        assert not match("Applied Research Intern, NLP - Fall 2026")
        assert PAY.search("The hourly rate for our interns is 20 USD - 71 USD.")[0] == "20 USD - 71 USD"
        assert PAY.search("pay range $94000 - $125000 plus")[0] == "$94000 - $125000" and not PAY.search("since 2019 - 2026")
        assert tier("Redmond, WA") == 0 and tier("Austin, TX") == 1 and tier("Tucson, AZ") == 2
        assert detect("https://jobs.eu.lever.co/quantinuum/abc") == ("lever", "quantinuum", "api.eu.lever.co")
        assert detect("https://ats.rippling.com/rev-robotics/jobs/1") == ("rippling", "rev-robotics")
        assert detect("https://lifeattiktok.com/search/7602395891774802181") == TIKTOK
        assert not match("Mechanical Engineer Intern") and not match("Site Reliability Engineer Intern")
        assert keep("Electrical Engineering Intern", "US-VA-Manassas") and keep("RF Intern", "Torrance, CA")
        assert not keep("Electrical Engineering Intern", "Taiwan, Hsinchu") and not keep("RF Intern", "Toronto, ON, Canada")
        assert keep("Electrical Engineering Intern", "2 Locations") and keep("Hardware Intern", "US, CA, Santa Clara")
        assert keep("Hardware Co-op", "Seattle, WA") and not keep("Hardware Co-op", "Austin, TX")
        assert keep("Hardware Intern/Co-op", "Austin, TX") and keep("RF Intern", "Markham, ON, CA; Austin, TX, US")
        assert not keep("RF Intern", "Markham, ON, CA") and not keep("IT Intern", "Cork, CO, IE") and keep("RF Intern", "San Diego, CA, US")
        assert PREFERRED.search("Hillsboro, Oregon") and PREFERRED.search("Portland, OR, USA") and not PREFERRED.search("Denver, CO")
        assert detect("https://wd5.myworkdaysite.com/en-US/recruiting/guidewire/external/job/x") == ("wd", "wd5.myworkdaysite.com", "guidewire", "external")
        assert detect("https://intel.wd1.myworkdayjobs.com/en-us/External/job/x") == ("wd", "intel.wd1.myworkdayjobs.com", "intel", "External")
        assert detect("https://hdjq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1") == ("oracle", "hdjq.fa.us2.oraclecloud.com", "CX_1")
        assert detect("https://careers.amd.com/jobs/86868?icims=1") == ("jibe", "careers.amd.com")
        assert detect("https://careers.cisco.com/global/en/job/2008430") == ("phenom", "careers.cisco.com/global/en")
        assert detect("https://careers.qorvo.com/job/Richardson-X-TX-75081/1374272000/?ats=successfactors") == ("sfrmk", "careers.qorvo.com")
        assert detect("https://www.tesla.com/careers/search/job/x-269198") is None
        print("ok")
    elif sys.argv[1:2] == ["preview"]:  # build the alert for one company's current roles -> last_email.html (no send)
        b = detect(sys.argv[2]); now = datetime.datetime.now(datetime.timezone.utc)
        email([(u, pretty(c), t, l) for u, c, t, l in SCANNERS[b[0]](*b[1:]) if keep(t, l)], now, set(), send=False)
    elif sys.argv[1:] == ["dry"]:  # full scan, print results, no email, no seen.json
        main(dry=True)
    elif sys.argv[1:2] == ["try"]:  # python3 jobscan.py try <careers url>: preview what one company returns
        b = detect(sys.argv[2]); print(b)
        for j in SCANNERS[b[0]](*b[1:]): print("KEEP" if keep(j[2], j[3]) else "    ", j[2], "|", j[3])
    else:
        main()
