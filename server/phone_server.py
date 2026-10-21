#!/usr/bin/env python3
"""Serveur PhoneScreen 3DS v2 (Termux) : screenrecord + ffmpeg, beaucoup plus rapide.

Installation (une seule fois) :
    pkg install python android-tools ffmpeg

Debogage sans fil deja connecte (adb devices doit afficher "device").

Lancement :
    termux-wake-lock
    python phone_server.py
"""
import os
import re
import socket
import struct
import subprocess
import threading
import time

PORT = 8080
IMG_W, IMG_H = 222, 480      # doit correspondre a source/main.c
QUALITY = 6                  # qualite JPEG ffmpeg : 2 (meilleure) a 31 (pire)
MAX_FPS = 15                 # limite d'images par seconde envoyees a la 3DS
BX = (320 - IMG_W) // 2      # decalage horizontal de l'image sur l'ecran tactile
phone = {"w": 1080, "h": 2400}


def adb(*args):
    return subprocess.Popen(["adb", "shell", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_phone_size():
    out = subprocess.run(["adb", "shell", "wm", "size"],
                         capture_output=True, text=True).stdout
    found = re.findall(r"(\d+)x(\d+)", out)
    if found:  # la derniere valeur est "Override size" si la resolution a ete changee
        phone["w"], phone["h"] = map(int, found[-1])


class Frames:
    """Garde la derniere image JPEG recue."""

    def __init__(self):
        self.cond = threading.Condition()
        self.data = None
        self.seq = 0

    def put(self, data):
        with self.cond:
            self.data = data
            self.seq += 1
            self.cond.notify_all()

    def wait_new(self, last, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last, timeout)
            return self.data, self.seq

    def latest(self):
        with self.cond:
            return self.data, self.seq


class Capture:
    """screenrecord (H.264, encodeur materiel) -> ffmpeg -> images JPEG."""

    def __init__(self, frames):
        self.frames = frames
        self.stop_flag = threading.Event()
        self.procs = []

    def start(self):
        read_phone_size()
        subprocess.run(["adb", "shell", "pkill", "screenrecord"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass

    def run(self):
        cw = max(16, min(phone["w"], 540) // 16 * 16)
        ch = max(16, (phone["h"] * cw // phone["w"]) // 16 * 16)
        while not self.stop_flag.is_set():
            rec = subprocess.Popen(
                ["adb", "exec-out", "screenrecord", "--output-format=h264",
                 "--size", f"{cw}x{ch}", "--bit-rate", "4000000", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ff = subprocess.Popen(
                ["ffmpeg", "-loglevel", "quiet", "-fflags", "nobuffer",
                 "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
                 "-f", "h264", "-i", "pipe:0",
                 "-vf", f"scale={IMG_W}:{IMG_H}", "-q:v", str(QUALITY),
                 "-pix_fmt", "yuvj420p", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            rec.stdout.close()
            self.procs = [rec, ff]
            buf = bytearray()
            try:
                while not self.stop_flag.is_set():
                    chunk = os.read(ff.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        s = buf.find(b"\xff\xd8")
                        if s < 0:
                            buf.clear()
                            break
                        e = buf.find(b"\xff\xd9", s + 2)
                        if e < 0:
                            del buf[:s]
                            break
                        self.frames.put(bytes(buf[s:e + 2]))
                        del buf[:e + 2]
            finally:
                for p in (ff, rec):
                    try:
                        p.kill()
                    except OSError:
                        pass
                    p.wait()
            if not self.stop_flag.is_set():
                time.sleep(0.3)   # screenrecord s'arrete apres 3 min : on relance


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def to_phone(x, y):
    """Coordonnees de l'ecran tactile 3DS -> coordonnees du telephone.
    L'ecran tactile montre la moitie basse de l'image (lignes 240 a 479)."""
    fx = min(max((x - BX) / IMG_W, 0), 1)
    fy = min(max((240 + y) / IMG_H, 0), 1)
    return int(fx * (phone["w"] - 1)), int(fy * (phone["h"] - 1))


def handle_events(conn):
    start = None
    t0 = 0.0
    try:
        while True:
            p = recv_exact(conn, 5)
            if p is None:
                return
            kind = p[0]
            x = (p[1] << 8) | p[2]
            y = (p[3] << 8) | p[4]
            if kind == 1:                       # appui
                start, t0 = (x, y), time.time()
            elif kind == 3 and start:           # relache
                moved = abs(x - start[0]) + abs(y - start[1])
                ax, ay = to_phone(*start)
                if moved < 8:
                    adb("input", "tap", str(ax), str(ay))
                else:
                    bx, by = to_phone(x, y)
                    ms = int(max(80, min(1000, (time.time() - t0) * 1000)))
                    adb("input", "swipe", str(ax), str(ay), str(bx), str(by), str(ms))
                start = None
            elif kind == 4:                     # touches : 1 = retour, 2 = accueil
                adb("input", "keyevent", "4" if x == 1 else "3")
    except OSError:
        return


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(1)
    print(f"En attente de la 3DS sur le port {PORT}")

    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("3DS connectee :", addr[0])
        frames = Frames()
        cap = Capture(frames)
        cap.start()
        threading.Thread(target=handle_events, args=(conn,), daemon=True).start()
        last, t_sent, warned = -1, 0.0, False
        try:
            while True:
                data, seq = frames.wait_new(last, 3)
                if data is None:
                    if not warned:
                        print("Pas encore d'image : screenrecord/ffmpeg demarre ? (pkg install ffmpeg)")
                        warned = True
                    continue
                if seq == last:
                    continue                    # ecran fixe : rien de nouveau
                pause = 1 / MAX_FPS - (time.time() - t_sent)
                if pause > 0:
                    time.sleep(pause)
                    data, seq = frames.latest()  # toujours envoyer la plus recente
                conn.sendall(struct.pack(">I", len(data)) + data)
                last, t_sent = seq, time.time()
        except OSError:
            pass
        cap.stop()
        conn.close()
        print("3DS deconnectee")


if __name__ == "__main__":
    main()
