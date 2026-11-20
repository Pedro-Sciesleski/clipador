#!/usr/bin/env python3
"""
Clipador MVP: assiste uma live e gera cortes automaticamente enquanto ela acontece.

Como detecta momentos chave:
  1. Pico de chat (Twitch ou YouTube): muitas mensagens em poucos segundos, com peso extra
     para emotes/risadas (KEKW, LUL, kkkk, CLIP...).
  2. Pico de áudio: trecho bem mais alto que a média recente (gritos, risadas).
  3. Manual: aperte ENTER no terminal para cortar "agora".

Uso:
  python clipador.py https://www.twitch.tv/nomedocanal
  python clipador.py "https://www.youtube.com/watch?v=XXXX"

Requisitos: Python 3.9+, ffmpeg no PATH, `pip install -U streamlink yt-dlp`.
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time

LOCK = threading.Lock()
CHILDREN = []  # processos extras para encerrar no fim


def log(msg):
    with LOCK:
        print(time.strftime("[%H:%M:%S]"), msg, flush=True)


# --------------------------------------------------------------------------
# Gravação: streamlink -> ffmpeg -> segmentos .ts de poucos segundos
# --------------------------------------------------------------------------
class Recorder:
    def __init__(self, url, workdir, seg_len, quality, input_file=None):
        self.url, self.seg_len, self.quality, self.input_file = url, seg_len, quality, input_file
        self.segdir = os.path.join(workdir, "segments")
        os.makedirs(self.segdir, exist_ok=True)
        for f in os.listdir(self.segdir):
            os.remove(os.path.join(self.segdir, f))
        self.listpath = os.path.join(self.segdir, "list.csv")
        self.segments = []          # [(path, start_stream_s, end_stream_s)]
        self.t0 = None              # relógio de parede do início da gravação
        self.new_segment_callbacks = []
        self.procs = []

    def start(self):
        pattern = os.path.join(self.segdir, "seg_%06d.ts")
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if self.input_file:  # modo teste: simula live com arquivo local
            ff += ["-re", "-i", self.input_file]
            stdin = None
        else:
            sl = subprocess.Popen(
                [find_exe("streamlink") or "streamlink", "--stdout", "--twitch-disable-ads", self.url, self.quality],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.procs.append(sl)
            ff += ["-i", "pipe:0"]
            stdin = sl.stdout
        ff += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
               "-f", "segment", "-segment_time", str(self.seg_len),
               "-reset_timestamps", "1",
               "-segment_list", self.listpath, "-segment_list_type", "csv",
               pattern]
        self.procs.append(subprocess.Popen(ff, stdin=stdin))
        threading.Thread(target=self._watch, daemon=True).start()

    def alive(self):
        return all(p.poll() is None for p in self.procs)

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()

    def _watch(self):
        seen = 0
        while True:
            if self.t0 is None and any(f.endswith(".ts") for f in os.listdir(self.segdir)):
                self.t0 = time.time()
                log("Gravação iniciada.")
            if os.path.exists(self.listpath):
                with open(self.listpath, newline="") as fh:
                    rows = [r for r in csv.reader(fh) if len(r) >= 3]
                for r in rows[seen:]:
                    dur = float(r[2]) - float(r[1])
                    start = self.segments[-1][2] if self.segments else 0.0
                    seg = (os.path.join(self.segdir, r[0]), start, start + dur)
                    with LOCK:
                        self.segments.append(seg)
                    for cb in self.new_segment_callbacks:
                        cb(seg)
                seen = len(rows)
            time.sleep(0.5)

    def recorded_until(self):
        with LOCK:
            return self.segments[-1][2] if self.segments else 0.0

    def wall_to_stream(self, wall):
        return wall - (self.t0 or wall)

    def stream_to_wall(self, s):
        return (self.t0 or time.time()) + s

    def cleanup(self, keep_seconds):
        limit = self.recorded_until() - keep_seconds
        with LOCK:
            old = [s for s in self.segments if s[2] < limit]
            self.segments = [s for s in self.segments if s[2] >= limit]
        for path, _, _ in old:
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Cortador: junta os segmentos certos e recorta com NVENC (GPU)
# --------------------------------------------------------------------------
def pick_encoder():
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("ffmpeg não encontrado no PATH.")
    return "h264_nvenc" if "h264_nvenc" in out else "libx264"


class Clipper:
    def __init__(self, rec, outdir, before, after, cooldown):
        self.rec, self.outdir = rec, outdir
        self.before, self.after, self.cooldown = before, after, cooldown
        self.encoder = pick_encoder()
        self.queue = collections.deque()
        self.last_clip_end = -1e9
        os.makedirs(outdir, exist_ok=True)
        self.logpath = os.path.join(outdir, "cortes.csv")
        threading.Thread(target=self._worker, daemon=True).start()
        log(f"Encoder de vídeo: {self.encoder}")

    def trigger(self, wall_time, reason, score=0.0):
        if self.rec.t0 is None:
            return
        t = self.rec.wall_to_stream(wall_time)
        if t - self.before < self.last_clip_end and reason != "manual":
            return  # já está dentro de um corte recente
        if t < self.last_clip_end + self.cooldown and reason != "manual":
            return
        self.last_clip_end = t + self.after
        log(f">>> Momento detectado ({reason}, score {score:.1f}) em {t:.0f}s da gravação")
        self.queue.append((t, reason, score))

    def _worker(self):
        while True:
            if not self.queue:
                time.sleep(0.5)
                continue
            t, reason, score = self.queue[0]
            # espera a gravação passar do fim do corte
            deadline = time.time() + self.after + 60
            while self.rec.recorded_until() < t + self.after and time.time() < deadline:
                if not self.rec.alive():
                    break
                time.sleep(1)
            self.queue.popleft()
            try:
                self._cut(t, reason, score)
            except Exception as e:  # noqa
                log(f"Erro ao cortar: {e}")

    def _cut(self, t, reason, score):
        a, b = max(0.0, t - self.before), t + self.after
        with LOCK:
            segs = [s for s in self.rec.segments if s[2] > a and s[1] < b]
        if not segs:
            log("Sem segmentos para esse momento (foi limpo ou a live acabou).")
            return
        b = min(b, segs[-1][2])
        concat = os.path.join(self.rec.segdir, f"concat_{int(t)}.txt")
        with open(concat, "w") as fh:
            for p, _, _ in segs:
                fh.write(f"file '{os.path.abspath(p)}'\n")
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(self.rec.stream_to_wall(t)))
        out = os.path.join(self.outdir, f"{stamp}_{reason}.mp4")

        def run(enc):
            vopts = ["-c:v", enc] + (["-preset", "p5", "-cq", "21"] if "nvenc" in enc
                                     else ["-preset", "veryfast", "-crf", "21"])
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", concat,
                   "-ss", f"{a - segs[0][1]:.2f}", "-t", f"{b - a:.2f}",
                   *vopts, "-c:a", "aac", "-b:a", "160k",
                   "-movflags", "+faststart", out]
            return subprocess.run(cmd, capture_output=True, text=True)

        r = run(self.encoder)
        if r.returncode != 0 and self.encoder != "libx264":
            log("NVENC falhou, usando CPU (libx264) a partir de agora.")
            self.encoder = "libx264"
            r = run(self.encoder)
        os.remove(concat)
        if r.returncode != 0:
            log(f"ffmpeg falhou: {r.stderr.strip()[-300:]}")
            return
        new = not os.path.exists(self.logpath)
        with open(self.logpath, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["arquivo", "motivo", "score", "inicio_s", "fim_s"])
            w.writerow([os.path.basename(out), reason, f"{score:.1f}", f"{a:.0f}", f"{b:.0f}"])
        log(f"✔ Corte salvo: {out} ({b - a:.0f}s)")


# --------------------------------------------------------------------------
# Detector 1: chat da Twitch (IRC anônimo, sem login)
# --------------------------------------------------------------------------
HYPE = re.compile(r"(kekw|lul|omegalul|pog|poggers|kappa|clip|\bw\b|monka|lmao|lol|"
                  r"k{4,}|ha(ha)+|rs(rs)+|😂|🤣|💀|🔥|😱|:face-|:rolling)", re.I)


def msg_weight(text, paid=False):
    """Mensagem normal vale 1; risada/emote vale mais; Super Chat vale bem mais."""
    return 1.0 + 1.5 * min(len(HYPE.findall(text)), 2) + (3.0 if paid else 0.0)


def find_exe(name):
    """Acha um comando mesmo que a pasta Scripts do Python não esteja no PATH."""
    path = shutil.which(name)
    if path:
        return path
    base = os.path.dirname(sys.executable)
    for cand in (os.path.join(base, "Scripts", name + ".exe"), os.path.join(base, name)):
        if os.path.exists(cand):
            return cand
    return None


class ChatAnalyzer:
    """Recebe mensagens (de qualquer plataforma) e dispara quando o chat explode."""

    def __init__(self, clipper, window, mult, min_msgs, lag=0.0):
        self.clipper = clipper
        self.window, self.mult, self.min_msgs, self.lag = window, mult, min_msgs, lag
        self.events = collections.deque()   # (horário da mensagem, peso)
        self.started = time.time()
        threading.Thread(target=self._analyze, daemon=True).start()

    def add(self, t, weight):
        with LOCK:
            self.events.append((t, weight))

    def _analyze(self):
        baseline_span = 180
        while True:
            time.sleep(1)
            # "lag": o chat do YouTube chega em lotes, então olhamos um pouco atrás
            now = time.time() - self.lag
            with LOCK:
                while self.events and self.events[0][0] < now - baseline_span - self.window:
                    self.events.popleft()
                ev = list(self.events)
            if now - self.started < 60:
                continue  # aquecendo para conhecer o ritmo normal do chat
            recent = sum(p for t, p in ev if now - self.window <= t < now)
            older = sum(p for t, p in ev if t < now - self.window)
            span = max(1.0, min(baseline_span, now - self.started - self.window))
            baseline = older / span * self.window
            if recent >= self.min_msgs and recent >= self.mult * max(baseline, 1.0):
                score = recent / max(baseline, 1.0)
                # o chat reage alguns segundos DEPOIS do momento
                self.clipper.trigger(now - self.window / 2, "chat", score)


class TwitchChat:
    """Chat da Twitch via IRC anônimo (sem login)."""

    def __init__(self, channel, analyzer):
        self.channel, self.analyzer = channel.lower(), analyzer
        threading.Thread(target=self._irc, daemon=True).start()

    def _irc(self):
        while True:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=300)
                s.sendall(f"NICK justinfan{int(time.time()) % 99999}\r\n"
                          f"JOIN #{self.channel}\r\n".encode())
                log(f"Conectado ao chat da Twitch: #{self.channel}")
                buf = ""
                while True:
                    data = s.recv(4096).decode("utf-8", "ignore")
                    if not data:
                        raise ConnectionError("chat desconectou")
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG " in line:
                            self.analyzer.add(time.time(), msg_weight(line.split(" :", 1)[-1]))
            except Exception as e:  # noqa
                log(f"Chat: {e}. Reconectando em 5s...")
                time.sleep(5)


def parse_youtube_chat_line(line):
    """Uma linha do live_chat.json do yt-dlp -> [(horário, texto, é_superchat)]."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return []
    out = []
    for a in d.get("replayChatItemAction", {}).get("actions", []):
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
            try:
                t = int(r["timestampUsec"]) / 1e6
            except (KeyError, ValueError, TypeError):
                t = time.time()
            out.append((t, text, kind == "liveChatPaidMessageRenderer"))
    return out


class YouTubeChat:
    """Chat de live do YouTube: o yt-dlp grava o chat num arquivo e a gente lê conforme cresce."""

    def __init__(self, url, analyzer, workdir):
        self.url, self.analyzer = url, analyzer
        self.dir = os.path.join(workdir, "chat")
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        exe = find_exe("yt-dlp")
        self.cmd = [exe] if exe else [sys.executable, "-m", "yt_dlp"]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            proc = subprocess.Popen(self.cmd + [
                "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                "--no-playlist", "--no-part", "-o", os.path.join(self.dir, "chat.%(ext)s"),
                self.url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            CHILDREN.append(proc)
            log("Lendo o chat da live do YouTube...")
            self._tail(proc)
            err = (proc.stderr.read() or "").strip().splitlines()
            log(f"Chat do YouTube parou{': ' + err[-1] if err else ''}. Tentando de novo em 10s...")
            time.sleep(10)

    def _tail(self, proc):
        fh, buf, total = None, "", 0
        while True:
            if fh is None:
                files = glob.glob(os.path.join(self.dir, "*live_chat.json*"))
                if files:
                    fh = open(files[0], encoding="utf-8", errors="ignore")
            if fh:
                data = fh.read()
                if data:
                    buf += data
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        for t, text, paid in parse_youtube_chat_line(line):
                            self.analyzer.add(t, msg_weight(text, paid))
                            total += 1
                            if total == 1:
                                log("Recebendo mensagens do chat do YouTube.")
            if proc.poll() is not None and not data_pending(fh):
                if fh:
                    fh.close()
                return
            time.sleep(1)


def data_pending(fh):
    if not fh:
        return False
    pos = fh.tell()
    more = bool(fh.read(1))
    fh.seek(pos)
    return more


# --------------------------------------------------------------------------
# Detector 2: pico de volume do áudio (analisa cada segmento gravado)
# --------------------------------------------------------------------------
class AudioDetector:
    def __init__(self, rec, clipper, db_above):
        self.rec, self.clipper, self.db_above = rec, clipper, db_above
        self.history = collections.deque(maxlen=60)
        rec.new_segment_callbacks.append(self._on_segment)

    def _on_segment(self, seg):
        threading.Thread(target=self._analyze, args=(seg,), daemon=True).start()

    def _analyze(self, seg):
        path, start, end = seg
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vn",
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        if not m:
            return
        vol = float(m.group(1))
        with LOCK:
            hist = list(self.history)
            self.history.append(vol)
        if len(hist) < 10:
            return
        base = statistics.median(hist)
        if vol >= base + self.db_above:
            self.clipper.trigger(self.rec.stream_to_wall((start + end) / 2),
                                 "audio", vol - base)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Clipador MVP de lives")
    ap.add_argument("url", nargs="?", help="URL da live (Twitch, YouTube, Kick...)")
    ap.add_argument("--saida", default="cortes", help="pasta dos cortes")
    ap.add_argument("--qualidade", default="best", help="qualidade do streamlink (best, 720p60...)")
    ap.add_argument("--antes", type=float, default=35, help="segundos antes do momento")
    ap.add_argument("--depois", type=float, default=15, help="segundos depois do momento")
    ap.add_argument("--cooldown", type=float, default=30, help="intervalo mínimo entre cortes")
    ap.add_argument("--chat-mult", type=float, default=3.0,
                    help="quantas vezes acima do normal o chat precisa ficar")
    ap.add_argument("--chat-min", type=float, default=15, help="mínimo de msgs (ponderadas) na janela")
    ap.add_argument("--chat-janela", type=float, default=10, help="janela de análise do chat (s)")
    ap.add_argument("--audio-db", type=float, default=8.0,
                    help="dB acima da mediana para considerar pico de áudio (0 desliga)")
    ap.add_argument("--sem-chat", action="store_true", help="desliga detector de chat")
    ap.add_argument("--teste-arquivo", help="simula uma live usando um vídeo local")
    args = ap.parse_args()

    if not args.url and not args.teste_arquivo:
        ap.error("informe a URL da live")
    if not args.teste_arquivo and not find_exe("streamlink"):
        sys.exit("streamlink não encontrado. Rode: pip install streamlink")

    workdir = os.path.join(args.saida, ".tmp")
    rec = Recorder(args.url, workdir, seg_len=5, quality=args.qualidade,
                   input_file=args.teste_arquivo)
    clipper = Clipper(rec, args.saida, args.antes, args.depois, args.cooldown)

    url = args.url or ""
    twitch = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
    youtube = re.search(r"(youtube\.com|youtu\.be)", url)
    if not args.sem_chat and (twitch or youtube):
        analyzer = ChatAnalyzer(clipper, args.chat_janela, args.chat_mult, args.chat_min,
                                lag=0 if twitch else 10)
        if twitch:
            TwitchChat(twitch.group(1), analyzer)
        else:
            YouTubeChat(url, analyzer, workdir)
    elif not args.sem_chat:
        log("Chat só é suportado na Twitch e no YouTube; usando áudio + manual.")
    if args.audio_db > 0:
        AudioDetector(rec, clipper, args.audio_db)

    def manual():
        for _ in sys.stdin:
            clipper.trigger(time.time(), "manual", 0)
    threading.Thread(target=manual, daemon=True).start()

    rec.start()
    log("Rodando. Aperte ENTER para cortar manualmente, Ctrl+C para sair.")
    try:
        while rec.alive() or clipper.queue:
            time.sleep(2)
            rec.cleanup(keep_seconds=600)  # guarda só os últimos 10 min em disco
        log("A live terminou ou caiu.")
        while clipper.queue:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Encerrando...")
    finally:
        rec.stop()
        for p in CHILDREN:
            if p.poll() is None:
                p.terminate()
        time.sleep(1)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
