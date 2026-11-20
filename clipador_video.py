#!/usr/bin/env python3
"""
Clipador de vídeo: analisa um vídeo inteiro (link do YouTube ou arquivo local)
e salva os melhores momentos como cortes, ranqueados.

Sinais usados (o que estiver disponível):
  1. "Mais repetidos" do YouTube: o gráfico de trechos que o público mais reassiste.
  2. Replay do chat: em lives que viraram vídeo, os picos de mensagens.
     Risadas, emotes e Super Chats contam mais.
  3. Áudio: trechos bem mais altos que o entorno (gritos, risadas).

Uso:
  python clipador_video.py "https://www.youtube.com/watch?v=XXXX"
  python clipador_video.py "https://youtu.be/XXXX" --top 15
  python clipador_video.py meu_video.mp4

Requisitos: Python 3.9+, ffmpeg no PATH, `pip install yt-dlp`.
"""
import argparse
import bisect
import csv
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def fmt(t):
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m:02d}m{s:02d}s"


# --------------------------------------------------------------------------
# Download (yt-dlp)
# --------------------------------------------------------------------------
def ytdlp_cmd():
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        sys.exit("yt-dlp não encontrado. Rode: pip install -U yt-dlp")


def download(url, basedir, max_height, with_chat, cookies_browser):
    yt = ytdlp_cmd()
    extra = ["--cookies-from-browser", cookies_browser] if cookies_browser else []
    vid = subprocess.run(yt + extra + ["--print", "id", "--skip-download", url],
                         capture_output=True, text=True)
    if vid.returncode != 0 or not vid.stdout.strip():
        sys.exit(f"Não consegui ler o link:\n{vid.stderr.strip()[-500:]}")
    vid = vid.stdout.strip().splitlines()[0]
    folder = os.path.join(basedir, vid)
    os.makedirs(folder, exist_ok=True)

    video = [f for f in glob.glob(os.path.join(folder, "video.*"))
             if not f.endswith((".part", ".json", ".ytdl"))]
    if video:
        log(f"Vídeo já baixado antes: {video[0]}")
    else:
        log(f"Baixando vídeo (até {max_height}p)...")
        r = subprocess.run(yt + extra + [
            "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
            "--merge-output-format", "mp4",
            "--write-info-json", "--no-playlist",
            "-o", os.path.join(folder, "video.%(ext)s"), url])
        if r.returncode != 0:
            sys.exit("Falha no download do vídeo.")
        video = [f for f in glob.glob(os.path.join(folder, "video.*"))
                 if not f.endswith((".part", ".json", ".ytdl"))]

    chat = glob.glob(os.path.join(folder, "*live_chat.json"))
    if with_chat and not chat:
        log("Tentando baixar o replay do chat (só existe em lives que viraram vídeo)...")
        subprocess.run(yt + extra + [
            "--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-playlist",
            "-o", os.path.join(folder, "video.%(ext)s"), url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        chat = glob.glob(os.path.join(folder, "*live_chat.json"))

    info = os.path.join(folder, "video.info.json")
    return video[0], (chat[0] if chat else None), (info if os.path.exists(info) else None)


# --------------------------------------------------------------------------
# Sinais (todos viram uma série com 1 valor por segundo)
# --------------------------------------------------------------------------
def duration_of(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    return float(r.stdout.strip())


def audio_signal(path, n):
    """Volume (dB RMS) por segundo, calculado pelo ffmpeg."""
    log("Analisando o áudio...")
    meta = path + ".rms.txt"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-vn",
                    "-af", "aresample=8000,asetnsamples=n=8000:p=0,"
                           "astats=metadata=1:reset=1,"
                           f"ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file='{meta}'",
                    "-f", "null", "-"])
    vals = [-90.0] * n
    t = None
    if os.path.exists(meta):
        with open(meta) as fh:
            for line in fh:
                m = re.search(r"pts_time:([\d.]+)", line)
                if m:
                    t = int(float(m.group(1)))
                m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
                if m and t is not None and t < n:
                    v = m.group(1)
                    vals[t] = -90.0 if v == "-inf" else max(-90.0, float(v))
        os.remove(meta)
    return vals


HYPE = re.compile(r"(kkkk+|k{4,}|ha(ha)+|rs(rs)+|lol|lmao|kekw|lul|pog|clip|"
                  r"😂|🤣|💀|🔥|😱|:face-|:rolling)", re.I)


def chat_signal(path, n):
    """Mensagens (ponderadas) por segundo a partir do live_chat.json do yt-dlp."""
    log("Analisando o replay do chat...")
    vals = [0.0] * n
    total = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            act = d.get("replayChatItemAction", {})
            try:
                t = int(int(act.get("videoOffsetTimeMsec", -1)) / 1000)
            except (TypeError, ValueError):
                continue
            if not 0 <= t < n:
                continue
            for a in act.get("actions", []):
                item = a.get("addChatItemAction", {}).get("item", {})
                for kind, r in item.items():
                    if kind not in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
                        continue
                    text = ""
                    for run in r.get("message", {}).get("runs", []):
                        if "text" in run:
                            text += run["text"]
                        elif "emoji" in run:
                            sc = run["emoji"].get("shortcuts") or [""]
                            text += run["emoji"].get("emojiId", "") + sc[0]
                    w = 1.0 + 1.5 * min(len(HYPE.findall(text)), 2)
                    if kind == "liveChatPaidMessageRenderer":
                        w += 3.0
                    vals[t] += w
                    total += 1
    log(f"  {total} mensagens no chat.")
    return vals if total > 50 else None


def heatmap_signal(info_path, n):
    """Gráfico de 'mais repetidos' do YouTube (0 a 1)."""
    with open(info_path, encoding="utf-8") as fh:
        hm = json.load(fh).get("heatmap") or []
    if not hm:
        return None
    log("Usando o gráfico de 'mais repetidos' do YouTube.")
    vals = [0.0] * n
    for seg in hm:
        a, b = int(seg["start_time"]), int(seg["end_time"]) + 1
        for t in range(max(0, a), min(n, b)):
            vals[t] = max(vals[t], float(seg["value"]))
    return vals


# --------------------------------------------------------------------------
# Matemática simples: suavizar, comparar com o entorno, normalizar
# --------------------------------------------------------------------------
def smooth(x, w):
    if w <= 1:
        return list(x)
    c = [0.0]
    for v in x:
        c.append(c[-1] + v)
    h = w // 2
    n = len(x)
    return [(c[min(n, i + h + 1)] - c[max(0, i - h)]) / (min(n, i + h + 1) - max(0, i - h))
            for i in range(n)]


def local_baseline(x, half=150, step=5):
    """Mediana dos ~5 min ao redor de cada ponto (o 'normal' daquele trecho)."""
    n = len(x)
    out = [0.0] * n
    anchors = list(range(0, n, 10)) or [0]
    meds = []
    for a in anchors:
        win = x[max(0, a - half):min(n, a + half):step]
        meds.append(statistics.median(win) if win else 0.0)
    for i in range(n):
        out[i] = meds[min(len(meds) - 1, i // 10)]
    return out


def robust_z(x, min_spread=1e-6):
    """Quantos 'desvios' cada ponto está acima do normal (resistente a outliers)."""
    med = statistics.median(x)
    mad = statistics.median([abs(v - med) for v in x])
    spread = max(1.4826 * mad, min_spread)
    return [(v - med) / spread for v in x]


def clamp(x, lo=-3, hi=8):
    return [max(lo, min(hi, v)) for v in x]


# --------------------------------------------------------------------------
# Escolher os melhores momentos e cortar
# --------------------------------------------------------------------------
def pick_peaks(score, top, gap, min_score):
    order = sorted(range(len(score)), key=lambda i: score[i], reverse=True)
    chosen = []
    for i in order:
        if score[i] < min_score or len(chosen) >= top:
            break
        # se o pico for um "platô", usa o meio dele
        lo = hi = i
        while lo > 0 and score[lo - 1] >= score[i] - 0.05:
            lo -= 1
        while hi < len(score) - 1 and score[hi + 1] >= score[i] - 0.05:
            hi += 1
        c = (lo + hi) // 2
        if all(abs(c - j) >= gap for j in chosen):
            chosen.append(c)
    return chosen


def pick_encoder():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    return "h264_nvenc" if "h264_nvenc" in out else "libx264"


def cut(src, a, b, out, encoder):
    def run(enc):
        v = ["-c:v", enc] + (["-preset", "p5", "-cq", "21"] if "nvenc" in enc
                             else ["-preset", "veryfast", "-crf", "21"])
        return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                               "-ss", f"{a:.2f}", "-i", src, "-t", f"{b - a:.2f}",
                               *v, "-c:a", "aac", "-b:a", "160k",
                               "-movflags", "+faststart", out],
                              capture_output=True, text=True)
    r = run(encoder)
    if r.returncode != 0 and encoder != "libx264":
        log("NVENC falhou, usando CPU (libx264).")
        encoder = "libx264"
        r = run(encoder)
    if r.returncode != 0:
        log(f"ffmpeg falhou: {r.stderr.strip()[-300:]}")
    return encoder


def main():
    ap = argparse.ArgumentParser(description="Clipador de vídeos do YouTube / arquivos")
    ap.add_argument("entrada", help="link do YouTube ou caminho de um vídeo")
    ap.add_argument("--top", type=int, default=10, help="quantos cortes gerar")
    ap.add_argument("--antes", type=float, default=35, help="segundos antes do pico")
    ap.add_argument("--depois", type=float, default=15, help="segundos depois do pico")
    ap.add_argument("--min-score", type=float, default=1.5,
                    help="nota mínima para virar corte (maior = mais exigente)")
    ap.add_argument("--saida", default="cortes", help="pasta dos cortes")
    ap.add_argument("--downloads", default="downloads", help="pasta dos vídeos baixados")
    ap.add_argument("--qualidade", type=int, default=1080, help="altura máxima do vídeo")
    ap.add_argument("--sem-chat", action="store_true", help="não baixa/usa o chat")
    ap.add_argument("--cookies-do-navegador", metavar="NAVEGADOR",
                    help="ex: chrome, firefox, edge (para vídeos que pedem login)")
    ap.add_argument("--chat", help="arquivo live_chat.json (para arquivo local)")
    ap.add_argument("--info", help="arquivo .info.json (para arquivo local)")
    args = ap.parse_args()

    if re.match(r"https?://", args.entrada):
        video, chat_path, info_path = download(args.entrada, args.downloads, args.qualidade,
                                               not args.sem_chat, args.cookies_do_navegador)
    else:
        if not os.path.exists(args.entrada):
            sys.exit(f"Arquivo não encontrado: {args.entrada}")
        video, chat_path, info_path = args.entrada, args.chat, args.info
    if args.sem_chat:
        chat_path = None

    n = int(duration_of(video)) + 1
    log(f"Vídeo: {video} ({fmt(n)})")

    # Cada sinal vira "quão acima do normal" está aquele segundo, com peso.
    parts = {}  # nome -> (peso, série z)
    audio = smooth(audio_signal(video, n), 3)
    base = local_baseline(audio)
    # piso de 1 dB: variações menores que isso não contam como pico
    parts["audio"] = (1.0, clamp(robust_z([a - b for a, b in zip(audio, base)], 1.0)))

    if chat_path and os.path.exists(chat_path):
        c = chat_signal(chat_path, n)
        if c:
            c = smooth(c, 10)
            # o chat reage alguns segundos depois: puxa o sinal ~6s para trás
            c = c[6:] + [0.0] * 6
            base = local_baseline(c)
            # msgs numa janela de 10s; compara com a oscilação natural (tipo Poisson)
            z = [(10 * a - 10 * b) / (2 * (10 * b + 4) ** 0.5) for a, b in zip(c, base)]
            parts["chat"] = (1.5, clamp(z))
    else:
        log("Sem replay de chat para esse vídeo.")

    if info_path and os.path.exists(info_path):
        h = heatmap_signal(info_path, n)
        if h:
            parts["repetidos"] = (1.5, clamp(robust_z(h)))
    if "repetidos" not in parts:
        log("Sem gráfico de 'mais repetidos' (vídeos novos ou com poucas views não têm).")

    total_w = sum(w for w, _ in parts.values())
    score = [sum(w * s[i] for w, s in parts.values()) / total_w for i in range(n)]
    peaks = pick_peaks(score, args.top, args.antes + args.depois, args.min_score)
    if not peaks:
        log("Nenhum momento passou da nota mínima. Tente --min-score 1.0")
        return
    log(f"Sinais usados: {', '.join(parts)}. {len(peaks)} momentos escolhidos.")

    name = os.path.splitext(os.path.basename(video))[0]
    if name == "video":
        name = os.path.basename(os.path.dirname(video))
    outdir = os.path.join(args.saida, name)
    os.makedirs(outdir, exist_ok=True)
    encoder = pick_encoder()
    log(f"Encoder de vídeo: {encoder}")

    rows = []
    for rank, p in enumerate(peaks, 1):
        a, b = max(0.0, p - args.antes), min(float(n - 1), p + args.depois)
        out = os.path.join(outdir, f"{rank:02d}_{fmt(p)}_nota{score[p]:.1f}.mp4")
        detail = ", ".join(f"{k} {s[p]:+.1f}" for k, (_, s) in parts.items())
        log(f"#{rank} em {fmt(p)} (nota {score[p]:.1f}: {detail})")
        encoder = cut(video, a, b, out, encoder)
        rows.append([rank, os.path.basename(out), fmt(a), fmt(b), f"{score[p]:.2f}", detail])

    with open(os.path.join(outdir, "relatorio.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "arquivo", "inicio", "fim", "nota", "sinais"])
        w.writerows(rows)
    log(f"✔ Pronto! {len(rows)} cortes em {outdir}")


if __name__ == "__main__":
    main()
