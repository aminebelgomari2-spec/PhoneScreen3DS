#!/usr/bin/env python3
"""Serveur PhoneScreen 3DS (a lancer dans Termux sur le telephone).

Installation (une seule fois) :
    pkg install python python-pillow android-tools

Debogage sans fil (Android 11+) :
    Parametres > Options pour les developpeurs > Debogage sans fil
    adb pair localhost:PORT_DE_JUMELAGE      (puis saisir le code)
    adb connect localhost:PORT_DU_DEBOGAGE
    adb devices                               (doit afficher "device")

Lancement :
    termux-wake-lock
    python phone_server.py
Puis sur la 3DS : entrer l'IP du telephone et appuyer sur A.
"""
import io
import socket
import struct
import subprocess
import threading
import time

from PIL import Image

PORT = 8080
IMG_W, IMG_H = 222, 480      # doit correspondre a source/main.c
QUALITY = 55
FPS = 5
BX = (320 - IMG_W) // 2      # decalage horizontal de l'image sur l'ecran tactile
phone = {"w": 1080, "h": 2400}


def adb(*args):
    return subprocess.Popen(["adb", "shell", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grab_jpeg():
    raw = subprocess.run(["adb", "exec-out", "screencap"], capture_output=True).stdout
    if len(raw) < 16:
        raise RuntimeError("capture vide : adb est-il connecte ? (adb devices)")
    w, h = struct.unpack("<II", raw[:8])
    header = len(raw) - w * h * 4
    if header < 12 or header > 64:
        raise RuntimeError("format screencap inattendu")
    phone["w"], phone["h"] = w, h
    img = Image.frombuffer("RGBA", (w, h), raw[header:], "raw", "RGBA", 0, 1).convert("RGB")
    img = img.resize((IMG_W, IMG_H), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY)
    return buf.getvalue()


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
    print(f"En attente de la 3DS sur le port {PORT} (IP du telephone : voir Parametres > WiFi)")

    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("3DS connectee :", addr[0])
        threading.Thread(target=handle_events, args=(conn,), daemon=True).start()
        try:
            while True:
                t = time.time()
                try:
                    jpg = grab_jpeg()
                except RuntimeError as e:
                    print("Erreur :", e)
                    time.sleep(1)
                    continue
                conn.sendall(struct.pack(">I", len(jpg)) + jpg)
                time.sleep(max(0, 1 / FPS - (time.time() - t)))
        except OSError:
            pass
        conn.close()
        print("3DS deconnectee")


if __name__ == "__main__":
    main()
