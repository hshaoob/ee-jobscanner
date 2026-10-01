#!/usr/bin/env python3
"""Hourly EE internship scanner. Polls company career sites / ATS APIs directly, emails new US matches."""
import collections, datetime, html, json, os, re, smtplib, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).parent
SEEN, BOARDS, COMPANIES, UNSCANNABLE = HERE / "seen.json", HERE / "boards.json", HERE / "companies.txt", HERE / "unscannable.txt"
NAMES = HERE / "names.json"  # board -> company display name
QUEUE = HERE / "queue.json"  # matches waiting for the next batch of 30
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


# Unmistakably EE in the title: enough on its own. Any other title (generic "Engineering Intern", "Test", "Systems",
# "Energy", ...) is only sent if the posting's own description asks for EE (EE_DESC).
CORE_EE = re.compile(r"electrical|electronic|hardware|\bpcb|analog|mixed[- ]signal|power(?! ?bi\b| ?apps| ?platform)|\brf\b|"
    r"microwave|antenna|signal integrity|\bsi\b|\bpi\b|\brtl\b|asic|\bsoc\b|digital design|design verification|\bdv\b|"
    r"physical design|silicon|circuit|\bic\b|semiconductor|fpga|embedded|firmware|\bhil\b|mechatronic|avionic|harness|ewis|"
    r"\bemi\b|\bemc\b|substation|\bp&c\b|\bt&d\b|grid|low[- ]voltage|\bdsp\b|signal processing|wireless|photonic|"
    r"instrumentation|\bi&c\b|controls? eng|layout (?:design|eng)|\bev\b|electric vehicle|\bbms\b|high[- ]voltage|"
    r"batter(?:y|ies) (?:systems?|pack|management|electrical|integration|test)|transmission (?:&|and) distribution|"
    r"protection (?:&|and) control|\brelay|\bpv\b|photovoltaic|inverter|\bate\b|radar|\bplc|\bscada|"
    r"controls? (?:&|and) automation|automation (?:&|and) controls?", re.I)
EE_DESC = re.compile(r"electrical|electronics\b|circuit|\bpcb|embedded|firmware|fpga|oscilloscope|analog|\bee\b|\bplc|"
    r"power (?:systems|electronics)|\brf\b|antenna|avionic|wire harness|\bhil\b|signal integrity|\bbms\b|high[- ]voltage", re.I)
# Not EE work unless the title also says so ("Electrical Manufacturing Intern" passes, "Manufacturing Engineering Intern" doesn't)
OTHER_DISCIPLINE = re.compile(r"mechanical|chemical|industrial|environmental|biomedical|structural|geotech|petroleum|"
    r"materials|process|business|operations|supply|sourcing|finance|manufactur|quality|production|assembly|construction|"
    r"estimat|program manag|project manag|procurement|purchasing|supplier|technician|facilit|safety|maintenance|"
    r"logistics|warehouse|planner|buyer|transportation|roadway|transit|mining|naval|marine|water|fire protection|packaging|"
    r"compliance|inventory|budget|project control|\bsre\b|observability|infrastructure", re.I)
MAX_AGE = 14  # days; older or undated postings are never sent


GRAD = re.compile(r"ph\.?d|doctoral|post-?doc|master'?s|\bmba\b|\bms\b|\bm\.s\.|(?<!under)graduate|residency", re.I)
UNDERGRAD = re.compile(r"undergrad|bachelor|\bbs\b|\bb\.s\.", re.I)
NOT_HW = re.compile(r"software|\bai\b|artificial intelligence|machine learning|\bml\b|deep learning|data (?:engineer|platform)|"
    r"applied scien|research scien|\bdata (?!cent)|recommendation|agentic|python|\bllm|\bnlp\b", re.I)
HW = re.compile(r"embedded|firmware|hardware|electrical|fpga|asic|silicon|circuit", re.I)


def tier(loc): return 0 if seattle(loc) else 1 if PREFERRED.search(loc) else 2  # Seattle > preferred > rest


def match(t):
    if GRAD.search(t) and not UNDERGRAD.search(t): return False  # undergrad roles only
    sw = NOT_HW.search(t) and not HW.search(t)
    ee = CORE_EE.search(t) or (not OTHER_DISCIPLINE.search(t) and (EE.search(t) or re.search(r"engineer", t, re.I)))
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
            # Phenom's own job pages can show "no longer available" for live roles (RTX); prefer the real Workday posting
            out += [((j.get("applyUrl") or "").removesuffix("/apply") if "myworkday" in (j.get("applyUrl") or "")
                     else f"https://{base}/job/{j['jobId']}", host, j["title"],
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


CLOSED = re.compile(r"no longer (?:accepting applications|available|open|active|posted)|(?:position|job|role|requisition) (?:has been|is) "
                    r"(?:filled|closed|expired)|job (?:you are looking for|you're looking for) (?:is|was|has)", re.I)


def details(url):
    """-> (posted datetime|None, pay str|None, company str|None, description text) from the job's own page/API. Best effort.
    Description None means the posting is gone (404/410, past its end date, or says it is closed)."""
    try:
        if m := re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url):
            d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}?pay_transparency=true", timeout=15, tries=1)
            rng = (d.get("pay_input_ranges") or [{}])[0]
            pay = rng and rng.get("min_cents") and f"${rng['min_cents'] / 100:,.0f} – ${rng['max_cents'] / 100:,.0f}"
            text = html.unescape(re.sub(r"<[^>]+>|&lt;[^&]*&gt;", " ", html.unescape(d.get("content", ""))))
            return (datetime.datetime.fromisoformat(d.get("first_published") or d["updated_at"]),
                    pay or (PAY.search(text) or [None])[0], d.get("company_name"), text)
        if m := re.search(r"https://([\w.-]+myworkday(?:jobs|site)\.com)/(?:recruiting/([\w-]+)/)?([\w-]+)(/job/.*)", url):
            host, tenant, site, path = m.groups()
            tenant = tenant or host.split(".")[0]
            info = fetch(f"https://{host}/wday/cxs/{tenant}/{site}{path}", timeout=15, tries=1)["jobPostingInfo"]
            text = html.unescape(re.sub(r"<[^>]+>", " ", info.get("jobDescription", "")))
            return (datetime.datetime.fromisoformat(info["startDate"]).replace(tzinfo=PACIFIC) if info.get("startDate") else None,
                    (PAY.search(text) or [None])[0], None, text)
        if m := re.search(r"https://([\w.-]+oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([\w-]+)/job/(\d+)", url):
            it = fetch(f"https://{m[1]}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                       f"&finder=ById;Id=%22{m[3]}%22,siteNumber={m[2]}", timeout=15, tries=1)["items"]
            if not it: return None, None, None, None
            it = it[0]
            text = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(it.get(k) or "" for k in (
                "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))))
            p = it.get("ExternalPostedStartDate")
            return datetime.datetime.fromisoformat(p) if p else None, (PAY.search(text) or [None])[0], None, text
        if m := re.search(r"smartrecruiters\.com/([^/?#]+)/(\d+)", url):
            d = fetch(f"https://api.smartrecruiters.com/v1/companies/{m[1]}/postings/{m[2]}", timeout=15, tries=1)
            if d.get("active") is False: return None, None, None, None
            text = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(
                s.get("text") or "" for s in ((d.get("jobAd") or {}).get("sections") or {}).values())))
            p = d.get("releasedDate")
            return (datetime.datetime.fromisoformat(p.replace("Z", "+00:00")) if p else None,
                    (PAY.search(text) or [None])[0], (d.get("company") or {}).get("name"), text)
        if re.search(r"\.bamboohr\.com/careers/\d+$", url):
            d = fetch(url + "/detail", timeout=15, tries=1)["result"]["jobOpening"]
            text = html.unescape(re.sub(r"<[^>]+>", " ", d.get("description") or ""))
            p = d.get("datePosted")
            return datetime.datetime.fromisoformat(p) if p else None, (PAY.search(text) or [None])[0], None, text
        page = fetch(url + ("&" if "?" in url else "?") + "in_iframe=1" if ".icims.com/" in url else url, raw=True, timeout=15, tries=1)
        posted = pay = org = desc = None
        for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
            try: d = json.loads(block)
            except ValueError: continue
            d = next((x for x in (d if isinstance(d, list) else [d]) if "JobPosting" in str(x.get("@type"))), None)
            if not d: continue
            if d.get("datePosted"):
                posted = datetime.datetime.fromisoformat(d["datePosted"].replace("Z", "+00:00").replace("+0000", "+00:00"))
            if (vt := d.get("validThrough")) and vt[:10] < f"{datetime.date.today()}": return None, None, None, None
            org = (d.get("hiringOrganization") or {}).get("name")
            desc = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(d.get("description") or "")))
            v = ((d.get("baseSalary") or {}).get("value") or {})
            if v.get("minValue"):
                pay = f"${float(v['minValue']):,.2f} – ${float(v.get('maxValue') or v['minValue']):,.2f}".replace(".00", "") + \
                      (f" / {v['unitText'].lower()}" if v.get("unitText") else "")
        if not posted and (m := re.search(r'\\?"(?:datePosted|postDateInGMT|postingDate|createdOn|publishedAt|postedDate)\\?"\s*:\s*'
                                          r'\\?"(\d{4}-\d\d-\d\d(?:T[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)?)', page)):  # Apple, Rippling, ...
            posted = datetime.datetime.fromisoformat(m[1].replace("Z", "+00:00"))
        if not posted and (m := re.search(r'itemprop="datePosted" content="(\w{3} \w{3} \d\d [\d:]{8}) UTC (\d{4})"', page)):  # SuccessFactors
            posted = datetime.datetime.strptime(f"{m[1]} {m[2]}", "%a %b %d %H:%M:%S %Y").replace(tzinfo=datetime.timezone.utc)
        text = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S))
        if not desc and CLOSED.search(text): return None, None, None, None
        return posted, pay or (PAY.search(text) or [None])[0], org, desc or text
    except urllib.error.HTTPError as e:
        return (None, None, None, None if e.code in (404, 410) else "")
    except Exception:
        return None, None, None, ""


SOURCES = {"myworkday": "Workday", "greenhouse": "Greenhouse", "lever.co": "Lever", "ashbyhq": "Ashby", "smartrecruiters": "SmartRecruiters",
           "oraclecloud": "Oracle", "icims": "iCIMS", "taleo": "Taleo", "eightfold": "Eightfold", "workable": "Workable",
           "bamboohr": "BambooHR", "rippling": "Rippling"}


def job_id(u):
    """The company's own requisition/job number, pulled from the link — search it on their careers site to confirm."""
    m = re.search(r"_((?:JR|REQ|R)?-?\d{4,}[\w-]*?)(?:/apply)?$|[?&](?:job|gh_jid|jobId)=(\d+)|/details/(\d+)|/jobs?/(\d{4,})|"
                  r"/job/(\d{4,})|/j/([0-9A-F]{8,})|/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})|/(\d{6,})/?$", u)
    return next((g for g in m.groups() if g), None) if m else None


def source(u):
    host = urllib.parse.urlparse(u).netloc
    return next((v for k, v in SOURCES.items() if k in host), host.removeprefix("www."))


# Resume profile (skills only, no personal details): (label, pattern, weight). Title hits count double.
PROFILE = [
    ("Avionics", r"avionic", 3), ("Wire harness", r"harness|ewis|wiring|cable assembl", 3),
    ("HIL", r"hardware[- ]in[- ]the[- ]loop|\bhil\b|test ?bed", 3), ("Embedded / firmware", r"embedded|firmware|microcontroller|\bmcu\b|esp32|arduino", 2),
    ("PCB design", r"\bpcb|schematic|board layout|kicad|altium|orcad", 2), ("Mixed-signal / analog", r"mixed[- ]signal|analog|op[- ]?amp|\badc\b|\bdac\b", 2),
    ("DSP / signals", r"\bdsp\b|signal processing|\bfft\b|signals? and systems", 2), ("Hardware test & validation", r"validation|verification|hardware test|test engineer|bench test", 2),
    ("Electrical engineering", r"electrical engineer|\bee\b|electronics", 1), ("Propulsion / aerospace", r"propulsion|aerospace|rocket|flight|launch|space", 1),
    ("Systems integration", r"integration|systems engineer", 1), ("Manufacturing / DFM", r"\bdfm\b|manufactur|ipc|whma|620", 1),
    ("Zuken / Creo / Windchill", r"zuken|\be3\.series|creo|windchill|\bpdm\b", 2), ("Sensors", r"sensor|thermocouple|\brtd\b|transducer|encoder", 1),
    ("SPI / I2C / I2S", r"\bspi\b|\bi2c\b|\bi2s\b|\buart\b|\bcan bus\b", 1), ("C / C++", r"\bc\+\+|\bc/c\+\+|\bembedded c\b", 1),
    ("Python / MATLAB", r"python|matlab", 1), ("Lab instruments", r"oscilloscope|multimeter|function generator|soldering|lab equipment", 1),
    ("Root cause analysis", r"root cause|\brca\b|troubleshoot|failure analysis", 1), ("Controls", r"\bcontrols?\b|\bgnc\b|robotic", 1),
]
PROFILE = [(label, re.compile(pat, re.I), w) for label, pat, w in PROFILE]
GRAD_REQUIRED = re.compile(r"(?:pursuing|enrolled in|currently in)\s+(?:a\s+)?(?:master|ph\.?d|graduate)[^.]{0,60}(?:required|degree)|"
                           r"must be (?:a )?(?:master|ph\.?d|graduate) student", re.I)


def fit(title, text=""):
    """-> (score 0-100, label, top matching skills). Title matches weigh double; grad-only roles are penalized."""
    hits = [(label, w * (2 if rx.search(title) else 1)) for label, rx, w in PROFILE if rx.search(title) or rx.search(text or "")]
    score = min(100, sum(w for _, w in hits) * 6 - (35 if GRAD_REQUIRED.search(text or "") else 0))
    label = "Strong fit" if score >= 55 else "Good fit" if score >= 30 else "Stretch"
    return max(score, 0), label, [l for l, _ in sorted(hits, key=lambda h: -h[1])[:3]]


def pretty(c):
    if c.islower() and " " in c: return c.title()  # "trane technologies" -> "Trane Technologies"
    c = re.sub(r"^(?:careers?|jobs|corningjobs)[.-]|\.(?:com|org|net|io|ai)(?:/.*)?$", "", c)
    return c[:1].upper() + c[1:]


LOGOS = HERE / "logos.json"  # company -> logo URL ("" = none found); cached so each company is looked up once
ATS_HOSTS = ("myworkday", "greenhouse", "lever.co", "ashbyhq", "smartrecruiters", "oraclecloud", "icims", "taleo",
             "eightfold", "workable", "bamboohr", "rippling", "simplify", "applytojob", "paylocity", "jobvite", "avature")


def _png_size(b):
    return int.from_bytes(b[16:20], "big") if b[:4] == b"\x89PNG" else 0


def _get(u, timeout=10):
    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.url, r.read()


def site_icon(d):
    """Sharp icon straight from a company's own site: apple-touch-icon, else the biggest PNG icon its homepage declares."""
    for touch in (f"https://www.{d}/apple-touch-icon.png", f"https://{d}/apple-touch-icon.png"):
        try:
            final, b = _get(touch)
            if _png_size(b) >= 57: return final
        except Exception: pass
    try:
        base, page = _get(f"https://www.{d}/")
        links = re.findall(r"<link[^>]+rel=[\"'][^\"']*icon[^\"']*[\"'][^>]*>", page.decode("utf-8", "ignore"), re.I)
        hrefs = [urllib.parse.urljoin(base, html.unescape(h)) for l in links if (h := (re.search(r'href=["\']([^"\']+\.png[^"\']*)', l) or [0, 0])[1])]
        for h in sorted(hrefs, key=lambda h: -int((re.search(r"(\d{2,3})x\d{2,3}", h) or [0, 0])[1])):
            if _png_size(_get(h)[1]) >= 32: return h
    except Exception: pass
    return None


def logo(company, url, cache):
    """Company logo URL. Finds the company's web domain (career-site host, name.com, ATS tenant, Clearbit's company search),
    then takes the sharpest icon: Google's favicon if >=48px, else the site's apple-touch-icon, else any real favicon."""
    if cache.get(company): return cache[company]
    host = urllib.parse.urlparse(url).netloc.lower()
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    tenant = (host.split(".")[0].removeprefix("careers-") if any(a in host for a in ("myworkdayjobs", "icims", "eightfold"))
              else parts[0] if any(a in host for a in ("greenhouse", "lever.co", "ashbyhq")) and parts else "")
    squash = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    cands = [] if any(a in host for a in ATS_HOSTS) else [".".join(host.split(".")[-2:])]
    trusted = set(cands)  # domains we know belong to this company (vs. name.com guesses)
    acronym = (re.search(r"\(([A-Za-z&]{2,8})\)", company) or [None, ""])[1].replace("&", "")
    clean = re.sub(r"\(.*?\)|\b(?:Group|Company|Companies|Family of|Inc|LLC|USA|US|Solutions|Corporation|Corp)\b\.?", "", company).strip(" ,-")
    for q in dict.fromkeys(filter(None, (company, clean if " " in clean else None))):  # a lone generic word matches strangers
        try:  # Clearbit's company search still returns domains (its logo service is gone)
            hits = json.loads(_get("https://autocomplete.clearbit.com/v1/companies/suggest?query=" + urllib.parse.quote(q))[1])
            exact = [h["domain"] for h in hits[:3] if squash(h["name"]) in (squash(company), squash(clean))]  # exact name only
            cands += exact; trusted |= set(exact)
        except Exception: pass
    cands += [squash(clean) + ".com", squash(company) + ".com", squash(tenant) + ".com", acronym.lower() + ".com"]

    exact = {squash(company) + ".com", squash(clean) + ".com"} | trusted  # domains safe to keep searching after a blurry hit
    best = (0, "")  # (pixel size, url): keep the sharpest real logo found
    for d in dict.fromkeys(c for c in cands if len(c) > 5):  # most-trusted first; "ti.com" is a real domain
        if best[1] and d not in exact: break  # only name-exact domains may improve a logo we already have
        g = f"https://www.google.com/s2/favicons?domain={d}&sz=128"
        try: best = max(best, (_png_size(_get(g)[1]), g))
        except Exception:  # Google has no icon for it; only keep going if we know the domain is this company's
            if d not in trusted: continue
        if best[0] >= 96: break
        if gh := github_avatar(d, company): best = (400, gh); break
        if best[0] < 48 and (icon := site_icon(d)): best = max(best, (100, icon))
        if best[0] >= 48: break
    if best[0] < 32 and (w := wikidata_logo(company)): best = (0, w)  # crisp official logo beats a blurry 16px favicon
    cache[company] = best[1]
    return cache[company]


def github_avatar(domain, company):
    """Square high-res logo from the company's GitHub org, only if the org lists this company's website."""
    head = {"User-Agent": UA, **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {})}
    for org in dict.fromkeys((re.sub(r"[^a-z0-9]", "", company.lower()), domain.split(".")[0])):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/orgs/{org}", headers=head), timeout=10) as r:
                o = json.load(r)
            if (urllib.parse.urlparse(o.get("blog") or "").netloc or o.get("blog") or "").removeprefix("www.").rstrip("/").endswith(domain):
                return o["avatar_url"] + "&s=128"
        except Exception: pass
    return None


def wikidata_logo(company):
    """Official logo (often a wide wordmark) from Wikidata/Commons, rendered as PNG. -> {"u", "w", "h"} or None."""
    head = {"User-Agent": "ee-jobscan/1.0 (personal internship alerts)"}
    get = lambda u: urllib.request.urlopen(urllib.request.Request(u, headers=head), timeout=15)
    try:
        hits = json.load(get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json&language=en&type=item&limit=3&search="
                             + urllib.parse.quote(company)))["search"]
        for h in hits:
            if not re.search(r"compan|corporat|manufactur|develop|firm|business|laborator|utility|contractor|provider|maker|agency",
                             h.get("description", ""), re.I): continue
            claims = json.load(get(f"https://www.wikidata.org/w/api.php?action=wbgetentities&format=json&props=claims&ids={h['id']}"))
            logo_file = claims["entities"][h["id"]]["claims"].get("P154", [{}])[0].get("mainsnak", {}).get("datavalue", {}).get("value")
            if not logo_file: continue
            with get("https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(logo_file) + "?width=200") as r:
                png, final = r.read(), r.url
            if png[:4] == b"\x89PNG": return {"u": final, "w": _png_size(png), "h": int.from_bytes(png[20:24], "big")}
    except Exception: pass
    return None


def ago(p, now):
    d = now - p
    if d < datetime.timedelta(hours=1): return "Just posted"
    if d < datetime.timedelta(days=1): return f"{d.seconds // 3600}h ago"
    if d.days < 14: return f"{d.days}d ago"
    return f"{d.days // 7} wks ago"


def email(jobs, seen_at, priority_names, send=True):
    """Alert email (best 30). Each card reads in a Z (Gutenberg): logo+role -> age, company/place/pay -> Apply.
    Only roles with a real post date within MAX_AGE days, still open, and EE work are sent.
    Returns the URLs handled (sent, or skipped on purpose); anything else carries over to the next batch."""
    esc = html.escape
    looked = sorted(jobs, key=lambda j: (tier(j[3]), j[1] not in priority_names))[:300]  # ponytail: 300 lookups/hour; the rest wait
    with ThreadPoolExecutor(32) as ex:
        info = dict(zip((j[0] for j in looked), ex.map(details, (j[0] for j in looked))))

    def posted(j):  # Simplify's date (sites we can't poll) > company's own date; never a guess
        p = POSTED.get(j[0]) or info[j[0]][0]
        return p.replace(tzinfo=datetime.timezone.utc) if p and p.tzinfo is None else p

    def age_days(j): return (seen_at - posted(j)).days if posted(j) else None

    def display(j):  # the posting's own org name if short, minus Inc/LLC
        org = info.get(j[0], (None, None, None))[2]
        return re.sub(r",? (?:Inc\.?|LLC|Corp\.?|Corporation)$", "", (org if org and len(org) < 40 else None) or j[1].split(" (via")[0])

    retry = {j[0] for j in looked if info[j[0]][3] == "" and not POSTED.get(j[0])}  # page didn't load: check again next hour
    ranked = [j for j in looked if j[0] not in retry and info[j[0]][3] is not None  # expired postings: handled, never sent
              and (a := age_days(j)) is not None and a <= MAX_AGE  # undated or too old: handled, never sent
              and (CORE_EE.search(j[2]) or EE_DESC.search(info[j[0]][3]))]  # generic title: the posting itself must ask for EE
    hidden = len(looked) - len(retry) - len(ranked)
    fits = {j[0]: fit(j[2], info[j[0]][3] or "") for j in ranked}
    # best fit first (Seattle still leads); at most 3 roles per company so applications look targeted, not scattershot
    ranked.sort(key=lambda j: (tier(j[3]) > 0, -fits[j[0]][0]))
    per_co, capped = collections.Counter(), []
    for j in ranked:
        per_co[display(j)] += 1
        if per_co[display(j)] <= 3: capped.append(j)
    dropped_same_co, ranked = len(ranked) - len(capped), capped
    buckets = [("Fresh", "Posted in the last 3 days", [j for j in ranked if age_days(j) <= 3]),
               ("Recent", f"Posted in the last {MAX_AGE} days", [j for j in ranked if age_days(j) > 3])]
    shown, cap = [], 30  # batches of 30 (~2.8KB a card keeps the email under Gmail's ~100KB clip); the rest waits for the next hour
    wanted = {j[0] for _, _, js in buckets for j in js}
    for i, (t, d, js) in enumerate(buckets):
        buckets[i] = (t, d, js[:max(0, cap - len(shown))]); shown += buckets[i][2]
    handled = {j[0] for j in looked} - retry - (wanted - {j[0] for j in shown})  # sent + skipped on purpose; the rest carries over
    if not shown:  # still email, so a missing alert always means the scan broke
        print("no new roles worth sending")
        if send: deliver("EE Internship Alert: scan finished, no new roles",
            f"<p>Hourly scan finished {seen_at.astimezone(PACIFIC):%-I:%M %p} PT. No new roles worth sending this hour.</p>")
        return handled
    waiting = len(wanted) - len(shown)
    cache = json.loads(LOGOS.read_text()) if LOGOS.exists() else {}
    firsts = {display(j): j for j in reversed(shown)}  # one lookup per company (parallel lookups of one company would race)
    with ThreadPoolExecutor(16) as ex: logos = dict(zip(firsts, ex.map(lambda kv: logo(kv[0], kv[1][0], cache), firsts.items())))
    LOGOS.write_text(json.dumps(cache, indent=0, sort_keys=True))

    INK, MUTED, FAINT, LINE = "#111827", "#6b7280", "#9ca3af", "#e5e7eb"
    INDIGO, EMERALD, AMBER = "#1f5f8b", "#1f7a5c", "#b07a2a"  # steel blue (pay, keywords) / action + fresh / attention (Seattle, recent)
    FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

    def card(j):
        u, c, t, loc = j
        _, pay, _, _ = info.get(u, (None, None, None, ""))
        score, fit_label, matched = fits[u]
        company, p, a = display(j), posted(j), age_days(j)
        if pay: pay = re.sub(r"(\d[\d,.]*) ?USD", r"$\1", pay).replace("$$", "$").replace(" - ", "–").replace(" – ", "–") \
            .replace(" to ", "–").replace("/Hr", "/hr").replace(" /", "/")
        loc = re.sub(r"(?i),? United States(?: of America)?|, USA?$|, US$", "", loc)
        loc = re.sub(r"^US-([A-Z]{2})-(.+)$", r"\2, \1", loc)  # "US-VA-Manassas" -> "Manassas, VA"
        if loc[:1].islower(): loc = re.sub(r"\b[a-z]", lambda m: m[0].upper(), loc)
        loc = loc if len(loc) < 60 else loc[:57] + "…"
        dot = EMERALD if a is not None and a <= 3 else AMBER if a is None or a <= 14 else FAINT
        age = ago(p, seen_at) if p else "Date not listed"
        age_style = f"font:700 12px/1.6 {FONT};color:{EMERALD}" if a is not None and a <= 3 else f"font:500 12px/1.6 {FONT};color:{MUTED}"
        # highlight the EE terms that made this role match (RF, PCB, power systems, FPGA, ...)
        title = "".join(f'<span style="background:#f5ecd2;padding:0 3px;border-radius:3px">{esc(x)}</span>' if i % 2 else esc(x)
                        for i, x in enumerate(re.split(f"((?:{EE.pattern})\\w*(?:\\s+(?:{EE.pattern})\\w*)*)", t, flags=re.I)) if x) if EE.search(t) else esc(t)
        place = f'<b style="color:{INK}">{esc(loc)}</b>' if loc and tier(loc) <= 1 else esc(loc or "Location not listed")
        meta = f'<span style="color:{INK}">{esc(company)}</span> &nbsp;&middot;&nbsp; {place}'
        if tier(loc) == 0: meta += (f' &nbsp;<span style="background:#f7efdf;color:#8a5a14;font:600 11px/1 {FONT};padding:3px 7px;'
                                    f'border-radius:999px;white-space:nowrap">SEATTLE AREA</span>')
        lg = logos.get(company)
        if isinstance(lg, dict):  # official wordmark: fit inside the logo slot without distortion
            k = min(44 / lg["w"], 36 / lg["h"])
            img = f'<img src="{esc(lg["u"])}" width="{round(lg["w"] * k)}" height="{round(lg["h"] * k)}" alt="{esc(company)}" style="display:block">'
        else: img = (f'<img src="{esc(lg)}" width="36" height="36" alt="" style="display:block;border-radius:8px;'
               f'border:1px solid {LINE};background:#fff">' if lg else
               f'<div style="width:36px;height:36px;border-radius:8px;background:#d9e8f3;color:{INDIGO};'
               f'font:600 14px/36px {FONT};text-align:center">{esc(company[:1].upper())}</div>')
        fit_color = {"Strong fit": EMERALD, "Good fit": INDIGO}.get(fit_label, MUTED)
        idline = (f"ID {job_id(u)} · " if job_id(u) else "") + ("Simplify" if "via Simplify" in c else source(u))
        return f"""<tr><td style="padding:18px 0;border-top:1px solid {LINE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
 <td width="48" valign="top">{img}</td>
 <td valign="top">
   <div style="font:600 16px/1.35 {FONT};color:{INK}">{title}</div>
   <div style="font:14px/1.5 {FONT};color:{MUTED};padding-top:4px">{meta}</div>
   <div style="font:13px/1.5 {FONT};padding-top:5px"><b style="color:{fit_color}">{fit_label} &middot; {score}%</b>
     <span style="color:{MUTED}">{esc(", ".join(matched)) if matched else "Few resume matches"}</span></div>
   <div style="font:11px/1.6 Menlo,Consolas,monospace;color:{FAINT};padding-top:2px">{esc(idline)}</div></td>
 <td width="130" valign="top" align="right" style="padding-left:12px">
   <div style="{age_style};white-space:nowrap"><span style="color:{dot}">&#9679;</span>&nbsp;{esc(age)}</div>
   {f'<div style="padding-top:4px"><span style="display:inline-block;background:#d9e8f3;color:{INDIGO};font:800 14px/1 {FONT};padding:5px 9px;border-radius:6px;white-space:nowrap">{esc(pay)}</span></div>' if pay else ""}
   <a href="{esc(u)}" style="display:inline-block;margin-top:10px;background:{EMERALD};color:#ffffff;font:600 13px/1 {FONT};
      text-decoration:none;padding:10px 18px;border-radius:8px;white-space:nowrap">Apply</a></td></tr></table></td></tr>"""

    label = {"Fresh": EMERALD, "Recent": AMBER}
    sections = "".join(
        f"""<tr><td style="padding:30px 0 10px"><span style="font:800 12px/1 {FONT};letter-spacing:.08em;text-transform:uppercase;color:{label.get(t, INDIGO)}">{t}</span>
<span style="font:12px/1 {FONT};color:{FAINT}">&nbsp; {len(js)} &nbsp;&middot;&nbsp; {d}</span></td></tr>{"".join(map(card, js))}"""
        for t, d, js in buckets if js)
    fresh_n, sea_n = len(buckets[0][2]), sum(tier(j[3]) == 0 for j in shown)
    headline = f"{fresh_n} fresh internship{'s' * (fresh_n != 1)}" if fresh_n else f"{len(shown)} new internship{'s' * (len(shown) != 1)}"
    summary = f"{len(shown)} role{'s' * (len(shown) != 1)} in this alert" + (f' &nbsp;&middot;&nbsp; <b style="color:#ecd6a8">{sea_n} in the Seattle area</b>' if sea_n else "")
    body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;background:#f3f4f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f7"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden">
<tr><td bgcolor="#34386b" style="padding:30px 32px 26px;background:#34386b;background-image:linear-gradient(135deg,#2c2f5c,#3f4480)">
  <div style="font:800 12px/1 {FONT};letter-spacing:.12em;text-transform:uppercase;color:#a7d7c5">EE Internship Alerts</div>
  <div style="font:800 26px/1.25 {FONT};color:#ffffff;padding-top:10px">{headline}</div>
  <div style="font:14px/1.5 {FONT};color:#d3d6ea;padding-top:6px">{summary}</div>
  <div style="font:13px/1.5 {FONT};color:#aab0d4">{seen_at.astimezone(PACIFIC):%A, %B %-d · %-I:%M %p} PT</div></td></tr>
<tr><td style="padding:4px 32px 8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{sections}</table></td></tr>
<tr><td style="padding:20px 32px 30px;border-top:1px solid {LINE};font:12px/1.6 {FONT};color:{FAINT}">
  Every role was live on the company's site when this was sent. Search the ID on their careers page to confirm it.
  {f"{hidden} roles were left out: posted over {MAX_AGE} days ago, no post date, closed, or not EE work." if hidden > 0 else ""}
  {f"{dropped_same_co} more roles at companies already listed were left out, to keep it to your best 3 per company." if dropped_same_co else ""}
  {f"{waiting} more matching roles are queued for the next alert." if waiting else ""}
  Fit % compares each role with your resume: title matches count double, and roles needing a graduate degree score lower.</td></tr>
</table></td></tr></table></body></html>"""
    top = list(dict.fromkeys(map(display, sorted(buckets[0][2] or shown, key=lambda j: -fits[j[0]][0]))))[:3]
    subject = f"New EE Internship Alert: {', '.join(top)}" + (f" · {fresh_n} fresh" if fresh_n else "")
    (HERE / "last_email.html").write_text(body)
    if not send: return print("Subject:", subject) or handled
    deliver(subject, body)
    return handled


def deliver(subject, body):
    wait = 20 * 60 - time.time() % 3600  # scan from :00, verify postings until ~:15, alert at :20 (or right away if running late)
    if os.environ.get("GITHUB_ACTIONS") and wait > 0: time.sleep(wait)
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
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}  # url -> when it was sent (or skipped on purpose)
    queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else {}  # url -> first seen, for matches waiting for a batch
    new = [j for j in jobs if j[0] not in seen]
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"{len(boards)} boards, {len(jobs)} matches, {len(new)} new or queued", flush=True)
    # send first (even with nothing new, as the hourly heartbeat): a failed send leaves everything queued for the next run
    try: handled = email(new, now, {names.get("|".join(b)) for b in boards if low(b) in priority})
    except Exception as e:  # record why (never the password itself) so a failed send is diagnosable from the repo
        got = f"FROM={'set' if FROM else 'MISSING'} TO={'set' if TO else 'MISSING'} PASS={len(os.environ.get('JOBSCAN_PASS', ''))} chars"
        (HERE / "last_run.txt").write_text(f"{now:%Y-%m-%dT%H:%M}Z EMAIL FAILED: {type(e).__name__}: {str(e)[:300]} | {got}\n")
        raise
    SEEN.write_text(json.dumps(seen | {u: now.isoformat(timespec="minutes") for u in handled}, indent=0))
    queue = {j[0]: queue.get(j[0], now.isoformat(timespec="minutes")) for j in new if j[0] not in handled}
    QUEUE.write_text(json.dumps(queue, indent=0))
    (HERE / "last_run.txt").write_text(f"{now.isoformat(timespec='minutes')} {len(jobs)} matches, {len(queue)} queued\n")  # keeps the repo active
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
        for t in ("Manufacturing Engineering Intern", "Quality Engineering Intern", "Process Engineering Intern",
                  "Test Technician Intern", "Construction Project Engineering Intern", "Supplier Quality Intern",
                  "Production Engineering Co-op", "Industrial Engineering Intern", "Estimating Intern"):
            assert not match(t), t
        assert match("Electrical Manufacturing Engineering Intern") and match("Signal Processing Intern")
        assert match("Test Engineering Intern") and not CORE_EE.search("Test Engineering Intern")  # needs EE in its description
        assert not CORE_EE.search("Power BI Intern") and CORE_EE.search("Power Systems Intern")
        for t in ("PCB Layout Design Intern", "HIL Validation Intern", "Signal Integrity Intern", "Wire Harness Design Intern",
                  "EV Systems Intern", "Battery Systems Engineering Intern", "BMS Engineering Intern", "High Voltage Engineering Intern",
                  "Power Electronics Intern", "Transmission & Distribution Intern", "Protection and Control Intern", "Solar PV Design Intern",
                  "Data Center Power Intern", "ASIC Design Intern", "Semiconductor Test Engineering Intern", "ATE Test Engineering Intern",
                  "Antenna Design Intern", "Radar Systems Intern", "Controls & Automation Intern", "PLC Programming Intern",
                  "SCADA Intern", "MEP Electrical Design Intern", "Medical Device Electrical Intern", "Robotics Electrical Intern"):
            assert match(t) and CORE_EE.search(t), t
        assert not match("Project Controls Intern") and not match("Battery Cell Manufacturing Intern")
        assert PAY.search("The hourly rate for our interns is 20 USD - 71 USD.")[0] == "20 USD - 71 USD"
        assert PAY.search("pay range $94000 - $125000 plus")[0] == "$94000 - $125000" and not PAY.search("since 2019 - 2026")
        assert job_id("https://nvidia.wd5.myworkdayjobs.com/X/job/US-CA/Hardware-Intern_JR2024692") == "JR2024692"
        assert job_id("https://job-boards.greenhouse.io/spacex/jobs/8616338002") == "8616338002"
        assert job_id("https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342717") == "342717"
        assert source("https://careers-gdms.icims.com/jobs/75140/x/job") == "iCIMS" and source("https://careers.amd.com/jobs/1") == "careers.amd.com"
        n = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)
        assert ago(n - datetime.timedelta(minutes=20), n) == "Just posted" and ago(n - datetime.timedelta(hours=5), n) == "5h ago"
        assert ago(n - datetime.timedelta(days=40), n) == "5 wks ago"
        assert re.sub(r"(\d[\d,.]*) ?USD", r"$\1", "42,000 USD - 88,000 USD").replace(" - ", " – ") == "$42,000 – $88,000"
        assert logo("Texas Instruments", "https://edbz.fa.us2.oraclecloud.com/x", {})
        assert "lockheedmartin.com" in logo("Lockheed Martin", "https://lockheedmartin.eightfold.ai/careers/job/1", {})
        assert re.split(f"((?:{EE.pattern})\\w*)", "Systems Engineering Intern", flags=re.I)[1] == "Systems Engineering"
        assert fit("Avionics Harness Design Intern", "Zuken E3 wire harness, IPC/WHMA-A-620, HIL test bed")[1] == "Strong fit"
        assert fit("Accounting Intern")[1] == "Stretch" and fit("Embedded Firmware Intern", "C/C++ SPI I2C oscilloscope")[1] != "Stretch"
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
        email([(u, pretty(c), t, l) for u, c, t, l in SCANNERS[b[0]](*b[1:]) if keep(t, l)], now, set(), send=sys.argv[3:] == ["send"])
    elif sys.argv[1:] == ["dry"]:  # full scan, print results, no email, no seen.json
        main(dry=True)
    elif sys.argv[1:2] == ["try"]:  # python3 jobscan.py try <careers url>: preview what one company returns
        b = detect(sys.argv[2]); print(b)
        for j in SCANNERS[b[0]](*b[1:]): print("KEEP" if keep(j[2], j[3]) else "    ", j[2], "|", j[3])
    else:
        main()
