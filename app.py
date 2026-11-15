import os, json, sqlite3, secrets, time, uuid
from datetime import datetime, timezone
from flask import Flask, request, session, jsonify, render_template_string, redirect, abort
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_SECURE=True,      # HTTPS only (Railway provides HTTPS)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
DB = os.environ.get("DB_PATH", "quiz.db")
QUIZ_HASH = generate_password_hash(os.environ["QUIZ_PASSWORD"])
ADMIN_HASH = generate_password_hash(os.environ["ADMIN_PASSWORD"])

# Correct answers stay on the server only.
QUESTIONS = [
    ("School", "Which organelle is known as the powerhouse of the cell?", ["Nucleus", "Mitochondria", "Ribosome", "Golgi body"], 1),
    ("School", "The basic unit of heredity is the:", ["Chromosome", "Gene", "Protein", "Enzyme"], 1),
    ("School", "The process by which plants make their own food is:", ["Respiration", "Photosynthesis", "Transpiration", "Digestion"], 1),
    ("School", "Which blood cells mainly fight infection?", ["Red blood cells", "Platelets", "White blood cells", "Plasma"], 2),
    ("DPT", "Which enzyme unwinds the DNA double helix during replication?", ["DNA ligase", "Helicase", "DNA polymerase", "Primase"], 1),
    ("DPT", "Which neurotransmitter is released at the neuromuscular junction?", ["Dopamine", "GABA", "Acetylcholine", "Serotonin"], 2),
    ("DPT", "The Krebs cycle takes place in the:", ["Cytoplasm", "Mitochondrial matrix", "Nucleolus", "Lysosome"], 1),
    ("DPT", "Which hormone lowers blood glucose levels?", ["Glucagon", "Insulin", "Cortisol", "Adrenaline"], 1),
    ("MBBS", "The start codon AUG codes for which amino acid?", ["Methionine", "Leucine", "Serine", "Stop signal"], 0),
    ("MBBS", "In which phase of the cell cycle does DNA synthesis occur?", ["G1", "S", "G2", "M"], 1),
]
CORRECT = [q[3] for q in QUESTIONS]

_fail = {}  # simple in-memory rate limiter for password attempts

def rate_limited(ip):
    count, first = _fail.get(ip, (0, time.time()))
    if time.time() - first > 900:
        count, first = 0, time.time()
    _fail[ip] = (count, first)
    return count >= 5

def record_fail(ip):
    count, first = _fail.get(ip, (0, time.time()))
    _fail[ip] = (count + 1, first)

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def q(sql, args=(), one=False):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()

with sqlite3.connect(DB) as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS attempts (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('active','locked','submitted')),
        started_at TEXT, ended_at TEXT,
        violation_at TEXT, violation_detail TEXT,
        answers TEXT NOT NULL DEFAULT '{}',
        score INTEGER)""")

def current_attempt():
    aid = session.get("attempt")
    if not aid:
        return None
    return q("SELECT * FROM attempts WHERE id=?", (aid,), one=True)

def finalize(attempt_id, status, detail=None):
    row = q("SELECT * FROM attempts WHERE id=?", (attempt_id,), one=True)
    if row["status"] != "active":
        return row
    answers = json.loads(row["answers"])
    score = sum(1 for i, c in enumerate(CORRECT) if answers.get(str(i)) == c)
    q("""UPDATE attempts SET status=?, ended_at=?, score=?,
         violation_at=?, violation_detail=? WHERE id=?""",
      (status, now(), score,
       now() if status == "locked" else None,
       (detail or "")[:300] if status == "locked" else None,
       attempt_id))
    return q("SELECT * FROM attempts WHERE id=?", (attempt_id,), one=True)

# ---------- Participant ----------

LOGIN_HTML = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Biology Quiz</title><style>
body{font-family:system-ui;margin:0;padding:24px;background:#f5f7fb;color:#222}
.card{max-width:420px;margin:40px auto;background:#fff;padding:24px;border-radius:12px;box-shadow:0 2px 8px #0001}
input{width:100%;box-sizing:border-box;font-size:18px;padding:12px;margin:8px 0;border:1px solid #ccc;border-radius:8px}
button{width:100%;font-size:18px;padding:14px;border:0;border-radius:8px;background:#1e6b52;color:#fff}
.err{color:#b00020}</style></head><body><div class=card>
<h2>Biology Examination</h2>
<p>Enter the access password to begin. You will have one attempt. Leaving the quiz screen will end the attempt.</p>
<form method=post action="/start"><input type=password name=password placeholder="Access password" required>
<button>Start quiz</button></form>{% if msg %}<p class=err>{{ msg }}</p>{% endif %}
</div></body></html>"""

@app.get("/")
def index():
    a = current_attempt()
    if a:
        return redirect("/quiz")
    return render_template_string(LOGIN_HTML, msg=request.args.get("msg"))

@app.post("/start")
def start():
    ip = request.remote_addr
    if rate_limited(ip):
        return render_template_string(LOGIN_HTML, msg="Too many attempts. Try again in 15 minutes.")
    if q("SELECT 1 FROM attempts LIMIT 1", one=True):
        return render_template_string(LOGIN_HTML, msg="This examination has already been used.")
    if not check_password_hash(QUIZ_HASH, request.form.get("password", "")):
        record_fail(ip)
        return render_template_string(LOGIN_HTML, msg="Incorrect password.")
    aid = str(uuid.uuid4())
    q("INSERT INTO attempts (id, status, started_at) VALUES (?, 'active', ?)", (aid, now()))
    session["attempt"] = aid
    return redirect("/quiz")

QUIZ_HTML = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Biology Quiz</title><style>
*{box-sizing:border-box;-webkit-user-select:none;user-select:none}
body{font-family:system-ui;margin:0;background:#f5f7fb;color:#222;padding-bottom:90px}
header{background:#1e6b52;color:#fff;padding:14px 16px;position:sticky;top:0}
.wrap{padding:16px;max-width:600px;margin:auto}
.level{font-size:13px;color:#1e6b52;font-weight:600}
.q{font-size:18px;line-height:1.4;margin:6px 0 14px}
.opt{display:block;width:100%;text-align:left;font-size:17px;padding:14px;margin:8px 0;border:2px solid #ccd;border-radius:10px;background:#fff}
.opt.sel{border-color:#1e6b52;background:#e6f2ed}
.dots{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}
.dot{width:34px;height:34px;border-radius:50%;border:1px solid #999;background:#fff;font-size:14px}
.dot.done{background:#1e6b52;color:#fff;border-color:#1e6b52}
.dot.cur{outline:2px solid #f0a500}
footer{position:fixed;bottom:0;left:0;right:0;background:#fff;display:flex;gap:8px;padding:12px;box-shadow:0 -2px 8px #0001}
footer button{flex:1;font-size:16px;padding:14px;border-radius:8px;border:0;background:#ddd}
footer .primary{background:#1e6b52;color:#fff}
#modal{display:none;position:fixed;inset:0;background:#0008;align-items:center;justify-content:center}
#modal .box{background:#fff;padding:20px;border-radius:12px;max-width:340px;margin:16px}
#modal .box button{width:100%;padding:12px;margin-top:8px;font-size:16px;border-radius:8px;border:0}
</style></head><body>
<header><b>Biology Examination</b> &middot; <span id=prog></span></header>
<div class=wrap>
  <div class=level id=lvl></div>
  <div class=q id=qtext></div>
  <div id=opts></div>
  <div class=dots id=dots></div>
</div>
<footer>
  <button id=prev>Previous</button>
  <button id=next>Next</button>
  <button id=sub class=primary>Submit</button>
</footer>
<div id=modal><div class=box><p id=mtext></p><button id=mok style="background:#1e6b52;color:#fff">Confirm</button><button id=mno style="background:#ddd">Cancel</button></div></div>
<script>
const QS = {{ questions|tojson }};
let cur = 0, answers = {}, done = false;
const $ = id => document.getElementById(id);

function render() {
  const item = QS[cur];
  $("prog").textContent = `Question ${cur+1} of ${QS.length}`;
  $("lvl").textContent = item.level + " level";
  $("qtext").textContent = item.q;
  $("opts").innerHTML = "";
  item.options.forEach((o, i) => {
    const b = document.createElement("button");
    b.className = "opt" + (answers[cur] === i ? " sel" : "");
    b.textContent = o;
    b.onclick = () => choose(i);
    $("opts").appendChild(b);
  });
  $("dots").innerHTML = "";
  QS.forEach((_, i) => {
    const d = document.createElement("button");
    d.className = "dot" + (answers[i] !== undefined ? " done" : "") + (i === cur ? " cur" : "");
    d.textContent = i + 1;
    d.onclick = () => { cur = i; render(); };
    $("dots").appendChild(d);
  });
  $("prev").disabled = cur === 0;
  $("next").disabled = cur === QS.length - 1;
}

async function choose(i) {
  answers[cur] = i;
  render();
  await fetch("/api/answer", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({q: cur, a: i})});
}

function ask(text) {
  return new Promise(res => {
    $("mtext").textContent = text;
    $("modal").style.display = "flex";
    $("mok").onclick = () => { $("modal").style.display = "none"; res(true); };
    $("mno").onclick = () => { $("modal").style.display = "none"; res(false); };
  });
}

async function finish(url) {
  done = true;
  const r = await fetch(url, {method:"POST", keepalive: true});
  location.href = "/result";
}

$("prev").onclick = () => { if (cur > 0) { cur--; render(); } };
$("next").onclick = () => { if (cur < QS.length - 1) { cur++; render(); } };
$("sub").onclick = async () => {
  const unanswered = QS.length - Object.keys(answers).length;
  const msg = unanswered ? `${unanswered} question(s) unanswered. Submit now? This cannot be undone.`
                         : "Submit your quiz now? This cannot be undone.";
  if (await ask(msg)) finish("/api/submit");
};

// Violation detection: any switch away from the quiz locks the attempt.
function violation(reason) {
  if (done) return;
  finish("/api/violation?reason=" + encodeURIComponent(reason));
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") violation("visibility_hidden");
});
window.addEventListener("blur", () => violation("window_blur"));
window.addEventListener("pagehide", () => violation("pagehide"));
window.addEventListener("beforeunload", e => { if (!done) { e.preventDefault(); e.returnValue = ""; } });

render();
</script></body></html>"""

@app.get("/quiz")
def quiz():
    a = current_attempt()
    if not a:
        return redirect("/")
    if a["status"] != "active":
        return redirect("/result")
    public = [{"level": lvl, "q": t, "options": opts} for lvl, t, opts, _ in QUESTIONS]
    return render_template_string(QUIZ_HTML, questions=public)

RESULT_HTML = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Your Result</title><style>
body{font-family:system-ui;background:#f5f7fb;margin:0;padding:24px;text-align:center}
.card{max-width:420px;margin:40px auto;background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 8px #0001}
.score{font-size:56px;font-weight:700;color:#1e6b52;margin:16px 0}
.note{color:#555;font-size:15px;line-height:1.5}
.warn{color:#b00020;font-weight:600}
</style></head><body><div class=card>
<h2>Biology Examination</h2>
<div class=score>{{ score }} / 10</div>
{% if status == 'locked' %}
<p class=warn>Your attempt was ended because you left the quiz screen.</p>
{% else %}
<p>Your answers have been submitted.</p>
{% endif %}
<p class=note>Attempt ended: {{ ended }}<br>Please take a screenshot of this page and send it to the examiner.</p>
</div></body></html>"""

@app.get("/result")
def result():
    a = current_attempt()
    if not a or a["status"] == "active":
        return redirect("/quiz")
    return render_template_string(RESULT_HTML, score=a["score"], status=a["status"], ended=a["ended_at"])

@app.post("/api/answer")
def api_answer():
    a = current_attempt()
    if not a or a["status"] != "active":
        abort(403)
    data = request.get_json(silent=True) or {}
    qi, ai = data.get("q"), data.get("a")
    if not (isinstance(qi, int) and isinstance(ai, int) and 0 <= qi < 10 and 0 <= ai < 4):
        abort(400)
    answers = json.loads(a["answers"])
    answers[str(qi)] = ai
    q("UPDATE attempts SET answers=? WHERE id=? AND status='active'", (json.dumps(answers), a["id"]))
    return jsonify(ok=True)

@app.post("/api/submit")
def api_submit():
    a = current_attempt()
    if not a:
        abort(403)
    finalize(a["id"], "submitted")
    return jsonify(ok=True)

@app.post("/api/violation")
def api_violation():
    a = current_attempt()
    if not a:
        abort(403)
    reason = request.args.get("reason", "unknown")[:100]
    finalize(a["id"], "locked", reason)
    return jsonify(ok=True)

# ---------- Admin ----------

ADMIN_LOGIN = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Admin</title></head><body style="font-family:system-ui;padding:24px">
<h3>Admin login</h3><form method=post><input type=password name=password required>
<button>Login</button></form>{% if msg %}<p>{{ msg }}</p>{% endif %}</body></html>"""

ADMIN_PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Admin</title><style>body{font-family:system-ui;padding:16px;font-size:14px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}</style>
</head><body><h3>Examination records</h3>
{% for r in rows %}
<table><tr><th>Status</th><td>{{ r.status }}</td></tr>
<tr><th>Score</th><td>{{ r.score }} / 10</td></tr>
<tr><th>Started</th><td>{{ r.started_at }}</td></tr>
<tr><th>Ended</th><td>{{ r.ended_at }}</td></tr>
<tr><th>Violation</th><td>{{ r.violation_at or '-' }} {{ r.violation_detail or '' }}</td></tr>
<tr><th>Answers (chosen / correct)</th><td>{% for i, c in qs %}Q{{ loop.index }}: {{ options[i][r.answers_map.get(i|string)] if r.answers_map.get(i|string) is not none else 'none' }} / {{ options[i][c] }}<br>{% endfor %}</td></tr>
</table><br>
{% else %}<p>No attempt yet.</p>{% endfor %}
<form method=post action="/admin/reset" onsubmit="return confirm('Delete all records and allow a new attempt?')">
<button>Reset examination</button></form>
<p><a href="/admin/logout">Logout</a></p></body></html>"""

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return redirect("/admin")
        return fn(*a, **k)
    return wrapper

from functools import wraps

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if check_password_hash(ADMIN_HASH, request.form.get("password", "")):
            session["admin"] = True
            return redirect("/admin/dashboard")
        return render_template_string(ADMIN_LOGIN, msg="Wrong password.")
    if session.get("admin"):
        return redirect("/admin/dashboard")
    return render_template_string(ADMIN_LOGIN, msg=None)

@app.get("/admin/dashboard")
@admin_required
def admin_dashboard():
    class Row:  # lightweight wrapper for template
        def __init__(self, r):
            self.__dict__.update(dict(r))
            self.answers_map = {k: v for k, v in json.loads(r["answers"]).items()}
    rows = [Row(r) for r in q("SELECT * FROM attempts ORDER BY started_at DESC")]
    options = [opts for _, _, opts, _ in QUESTIONS]
    qs = list(enumerate(CORRECT))
    return render_template_string(ADMIN_PAGE, rows=rows, options=options, qs=qs)

@app.post("/admin/reset")
@admin_required
def admin_reset():
    q("DELETE FROM attempts")
    return redirect("/admin/dashboard")

@app.get("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")

if __name__ == "__main__":
    app.run(debug=False)