// PhoneScreen 3DS - affiche l'ecran d'un telephone Android sur la 3DS
// et renvoie le toucher. Le telephone envoie une image de 222x480
// coupee en deux : moitie haute = ecran du haut, moitie basse = ecran tactile.

#include <3ds.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <malloc.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <turbojpeg.h>

#define PORT      8080
#define IMG_W     222          // doit correspondre a server/phone_server.py
#define IMG_H     480
#define MAX_JPEG  (256 * 1024)
#define SOC_SIZE  0x100000
#define IP_FILE   "sdmc:/3ds/phonescreen_ip.txt"

static u8 *socBuf;
static int sock = -1;
static tjhandle tj;

static u8 jpg[MAX_JPEG];
static u32 jpgLen, jpgGot;
static u8 hdr[4];
static int hdrGot;
static u8 rgb[IMG_W * IMG_H * 3];

/* ---------- reseau ---------- */

static int connect_server(const char *ip) {
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return -1;

    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(PORT);
    a.sin_addr.s_addr = inet_addr(ip);

    if (connect(sock, (struct sockaddr *)&a, sizeof a) < 0) {
        close(sock);
        sock = -1;
        return -1;
    }
    fcntl(sock, F_SETFL, fcntl(sock, F_GETFL, 0) | O_NONBLOCK);
    return 0;
}

// Evenement tactile/touche : 5 octets [type][x hi][x lo][y hi][y lo]
// type 1 = appui, 2 = deplacement, 3 = relache, 4 = touche (x: 1 retour, 2 accueil)
static void send_event(u8 type, u16 x, u16 y) {
    u8 p[5] = { type, x >> 8, x & 0xFF, y >> 8, y & 0xFF };
    send(sock, p, 5, 0);
}

// Retourne 1 si une image complete est dans jpg[], 0 si rien de complet, -1 si erreur
static int poll_frame(void) {
    for (;;) {
        if (hdrGot < 4) {
            int n = recv(sock, hdr + hdrGot, 4 - hdrGot, 0);
            if (n == 0) return -1;
            if (n < 0) return (errno == EWOULDBLOCK || errno == EAGAIN) ? 0 : -1;
            hdrGot += n;
            if (hdrGot < 4) continue;
            jpgLen = ((u32)hdr[0] << 24) | ((u32)hdr[1] << 16) | ((u32)hdr[2] << 8) | hdr[3];
            if (jpgLen == 0 || jpgLen > MAX_JPEG) return -1;
            jpgGot = 0;
        }
        int n = recv(sock, jpg + jpgGot, jpgLen - jpgGot, 0);
        if (n == 0) return -1;
        if (n < 0) return (errno == EWOULDBLOCK || errno == EAGAIN) ? 0 : -1;
        jpgGot += n;
        if (jpgGot == jpgLen) { hdrGot = 0; return 1; }
    }
}

/* ---------- affichage ---------- */

// Le framebuffer de la 3DS est stocke par colonnes, de bas en haut, en BGR.
static void blit(u8 *fb, int xoff, int row0) {
    for (int y = 0; y < 240; y++) {
        const u8 *s = rgb + (size_t)(row0 + y) * IMG_W * 3;
        for (int x = 0; x < IMG_W; x++, s += 3) {
            u8 *d = fb + ((xoff + x) * 240 + (239 - y)) * 3;
            d[0] = s[2];
            d[1] = s[1];
            d[2] = s[0];
        }
    }
}

static void draw_frame(void) {
    int w, h, ss, cs;
    if (tjDecompressHeader3(tj, jpg, jpgLen, &w, &h, &ss, &cs) != 0) return;
    if (w != IMG_W || h != IMG_H) return;
    if (tjDecompress2(tj, jpg, jpgLen, rgb, IMG_W, 0, IMG_H, TJPF_RGB, TJFLAG_FASTDCT) != 0) return;

    u8 *top = gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL);
    u8 *bot = gfxGetFramebuffer(GFX_BOTTOM, GFX_LEFT, NULL, NULL);
    blit(top, (400 - IMG_W) / 2, 0);
    blit(bot, (320 - IMG_W) / 2, 240);
    gfxFlushBuffers();
    gfxSwapBuffers();
}

/* ---------- session de streaming ---------- */

static void stream(void) {
    gfxSetScreenFormat(GFX_TOP, GSP_BGR8_OES);
    gfxSetScreenFormat(GFX_BOTTOM, GSP_BGR8_OES);
    gfxSetDoubleBuffering(GFX_TOP, false);
    gfxSetDoubleBuffering(GFX_BOTTOM, false);
    memset(gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL), 0, 400 * 240 * 3);
    memset(gfxGetFramebuffer(GFX_BOTTOM, GFX_LEFT, NULL, NULL), 0, 320 * 240 * 3);

    tj = tjInitDecompress();
    hdrGot = 0;
    u16 cx = 0, cy = 0, sx = 0, sy = 0;

    while (aptMainLoop()) {
        hidScanInput();
        u32 d = hidKeysDown(), h = hidKeysHeld(), u = hidKeysUp();
        if (d & KEY_START) break;

        if (h & KEY_TOUCH) {
            touchPosition t;
            hidTouchRead(&t);
            cx = t.px;
            cy = t.py;
            if (d & KEY_TOUCH) {
                send_event(1, cx, cy);
                sx = cx; sy = cy;
            } else if (abs((int)cx - sx) + abs((int)cy - sy) > 3) {
                send_event(2, cx, cy);
                sx = cx; sy = cy;
            }
        }
        if (u & KEY_TOUCH) send_event(3, cx, cy);
        if (d & KEY_B) send_event(4, 1, 0);   // retour
        if (d & KEY_X) send_event(4, 2, 0);   // accueil

        int r;
        while ((r = poll_frame()) == 1) draw_frame();
        if (r < 0) break;

        gspWaitForVBlank();
    }
    tjDestroy(tj);
}

/* ---------- menu / IP ---------- */

static void load_ip(char *ip) {
    FILE *f = fopen(IP_FILE, "r");
    if (!f) return;
    if (fgets(ip, 32, f)) ip[strcspn(ip, "\r\n")] = 0;
    fclose(f);
}

static void save_ip(const char *ip) {
    FILE *f = fopen(IP_FILE, "w");
    if (!f) return;
    fputs(ip, f);
    fclose(f);
}

static void ask_ip(char *ip) {
    SwkbdState kb;
    char buf[32];
    swkbdInit(&kb, SWKBD_TYPE_NORMAL, 2, 15);
    swkbdSetHintText(&kb, "IP du telephone (ex 192.168.1.20)");
    swkbdSetInitialText(&kb, ip);
    if (swkbdInputText(&kb, buf, sizeof buf) == SWKBD_BUTTON_CONFIRM) strcpy(ip, buf);
}

static void show_menu(const char *ip, const char *msg) {
    memset(gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL), 0, 400 * 240 * 3);
    consoleInit(GFX_BOTTOM, NULL);
    printf("PhoneScreen 3DS\n\n");
    printf("Serveur : %s:%d\n\n", ip, PORT);
    printf("A     : se connecter\n");
    printf("Y     : changer l'IP\n");
    printf("START : quitter\n\n");
    printf("En streaming :\n  B = retour, X = accueil,\n  START = deconnecter\n\n%s\n", msg);
}

int main(void) {
    gfxInitDefault();

    char ip[32] = "192.168.1.10";
    load_ip(ip);

    socBuf = memalign(0x1000, SOC_SIZE);
    int ok = socBuf && socInit(socBuf, SOC_SIZE) == 0;
    show_menu(ip, ok ? "" : "Erreur : sockets/WiFi indisponibles");

    while (aptMainLoop()) {
        hidScanInput();
        u32 k = hidKeysDown();
        if (k & KEY_START) break;

        if (ok && (k & KEY_Y)) {
            ask_ip(ip);
            save_ip(ip);
            show_menu(ip, "");
        }
        if (ok && (k & KEY_A)) {
            printf("Connexion...\n");
            gfxFlushBuffers(); gfxSwapBuffers(); gspWaitForVBlank();
            if (connect_server(ip) == 0) {
                stream();
                close(sock);
                sock = -1;
                show_menu(ip, "Deconnecte.");
            } else {
                show_menu(ip, "Connexion impossible (bonne IP ? serveur lance ?)");
            }
        }
        gfxFlushBuffers();
        gfxSwapBuffers();
        gspWaitForVBlank();
    }

    if (ok) socExit();
    free(socBuf);
    gfxExit();
    return 0;
}
