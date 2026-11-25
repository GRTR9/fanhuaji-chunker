"""核心邏輯測試：用假的 API session 取代繁化姬，不需連網。執行：python tests/test_core.py"""
import json, os, random, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import zhconv_core as z

TABLE = str.maketrans("简体这个说话们来时发与爱见对门东车长马鸟", "簡體這個說話們來時發與愛見對門東車長馬鳥")
class Resp:
    def __init__(s, d, code=200): s.d, s.status_code = d, code
    def json(s): return s.d
    def raise_for_status(s): pass
class Sess:
    def __init__(s, fail_every=0, limit=None): s.n=0; s.headers={}; s.fail_every=fail_every; s.limit=limit; s.sizes=[]
    def post(s, url, data, timeout):
        s.n += 1
        if s.fail_every and s.n % s.fail_every == 0: return Resp({}, 503)
        t = data["text"]; s.sizes.append(len(t))
        if s.limit and len(t) > s.limit: return Resp({"code": 1, "msg": "too long"})
        return Resp({"code": 0, "msg": "", "data": {"text": t.translate(TABLE)}})

opts = z.ConvertOptions(chunk_size=1000, delay=0, retries=3)
d = tempfile.mkdtemp()
# 1. plain text, long lines, mixed newlines
random.seed(1)
lines = []
for i in range(3000):
    n = random.choice([5, 50, 300, 2500])
    lines.append("".join(random.choice("简体这个说话们来时发。，！abc ") for _ in range(n)) + random.choice(["\n", "\r\n"]))
src = os.path.join(d, "a.txt"); open(src, "w", encoding="gb18030", newline="").write("".join(lines))
s = Sess(fail_every=7, limit=1000)
time.sleep = lambda x: None
z.convert_file(src, os.path.join(d, "a_tw.txt"), opts, session=s, log=lambda m: None)
out = open(os.path.join(d, "a_tw.txt"), encoding="utf-8", newline="").read()
assert out == "".join(lines).translate(TABLE), "plain mismatch"
assert max(s.sizes) <= 1000
assert not os.path.exists(os.path.join(d, "a_tw.txt.zhcache"))
print("plain ok, requests:", s.n)

# 2. resume after cancel
s2 = Sess(); cnt = {"n": 0}
def stop(): cnt["n"] += 1; return cnt["n"] > 40
try: z.convert_file(src, os.path.join(d, "b.txt"), opts, session=s2, should_stop=stop, log=lambda m: None)
except z.Cancelled: pass
assert os.path.exists(os.path.join(d, "b.txt.zhcache"))
s3 = Sess(); logs=[]
z.convert_file(src, os.path.join(d, "b.txt"), opts, session=s3, log=logs.append)
assert open(os.path.join(d, "b.txt"), encoding="utf-8", newline="").read() == out
print("resume ok:", [l for l in logs if "續傳" in l], "requests second run:", s3.n, "first:", s2.n)

# 3. json
obj = {"简体": ["这个\n说话", {"k": "们来", "n": 3, "b": True, "e": "abc"}], "x": None, "q": 'he said "爱" \\ ok'}
obj["list"] = ["这个说话" * 50 + str(i) for i in range(100)]
src = os.path.join(d, "c.json"); json.dump(obj, open(src, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
z.convert_file(src, os.path.join(d, "c_tw.json"), opts, session=Sess(), log=lambda m: None)
r = json.load(open(os.path.join(d, "c_tw.json"), encoding="utf-8"))
assert r["简体"][0] == "這個\n說話" and r["简体"][1]["k"] == "們來" and r["q"] == 'he said "愛" \\ ok' and r["list"][5].startswith("這個說話")
opts.json_convert_keys = True
z.convert_file(src, os.path.join(d, "d.json"), opts, session=Sess(), log=lambda m: None)
assert "簡體" in json.load(open(os.path.join(d, "d.json"), encoding="utf-8"))
print("json ok")

# 4. json with strings far longer than chunk_size (each request must stay under the limit)
opts.json_convert_keys = False
big = "".join(random.choice("简体这个说话们来时发。，！\n\\\"ab ") for _ in range(25000))
obj = {"big": big, "big2": "这个" * 3000, "small": "说话"}
src = os.path.join(d, "e.json"); json.dump(obj, open(src, "w", encoding="utf-8"), ensure_ascii=False)
s4 = Sess(limit=1000)
z.convert_file(src, os.path.join(d, "e_tw.json"), opts, session=s4, log=lambda m: None)
r = json.load(open(os.path.join(d, "e_tw.json"), encoding="utf-8"))
assert r["big"] == big.translate(TABLE) and r["big2"] == "這個" * 3000 and r["small"] == "說話"
assert max(s4.sizes) <= 1000
print("json long strings ok, requests:", s4.n)

# 5. server returns 502 for too-long requests -> auto split smaller, remember the size
class Sess502(Sess):
    def post(s, url, data, timeout):
        s.n += 1; t = data["text"]; s.sizes.append(len(t))
        if len(t) > 1000: return Resp({}, 502)
        return Resp({"code": 0, "msg": "", "data": {"text": t.translate(TABLE)}})
opts5 = z.ConvertOptions(chunk_size=8000, delay=0, retries=3)
src = os.path.join(d, "a.txt")
s5 = Sess502(); logs5 = []
z.convert_file(src, os.path.join(d, "f.txt"), opts5, session=s5, log=logs5.append)
assert open(os.path.join(d, "f.txt"), encoding="utf-8", newline="").read() == out
fails = sum(1 for x in s5.sizes if x > 1000)
assert fails < 20, fails          # only the first few oversize attempts, then it remembers
print("auto split ok, requests:", s5.n, "oversize attempts:", fails, [l for l in logs5 if "太長" in l][:1])
src = os.path.join(d, "e.json")
s6 = Sess502()
z.convert_file(src, os.path.join(d, "g.json"), opts5, session=s6, log=lambda m: None)
r = json.load(open(os.path.join(d, "g.json"), encoding="utf-8"))
assert r["big"] == big.translate(TABLE) and r["big2"] == "這個" * 3000
print("json auto split ok")
