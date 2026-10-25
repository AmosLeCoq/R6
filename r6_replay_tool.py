#!/usr/bin/env python3
"""
r6_replay_tool.py : reconstitue une manche entière à partir d'un replay Rainbow Six Siege (.rec) :
qui est où, à quel moment, et ce qui se passe (kills, messages), puis produit des CSV,
des images et un lecteur HTML interactif de la manche.

Usage :
    python r6_replay_tool.py Match-XXXX-R09.rec
    python r6_replay_tool.py Match-XXXX-R09.rec -o sortie
    python r6_replay_tool.py Match-XXXX-R09.rec --noms "0xf00d20d7=Decoy_99"

Dépendances :  pip install zstandard numpy matplotlib

Sorties (dossier -o, par défaut "sortie_<nom du replay>") :
    manche.html           lecteur interactif : carte vue de dessus, curseur de temps, lecture, événements
    evenements.csv        kills et messages datés (temps du replay, phase, chrono affiché en jeu)
    positions.csv         une ligne par position : joueur, opérateur, équipe, temps, x, y, z
    orientations.csv      direction du regard : lacet (horizontal) et, quand la caméra est trouvée, tangage
    trajectoires.png      vue d'ensemble, tous les joueurs nommés
    resume.txt            en-tête, horloge, correspondance entités -> joueurs, couverture des données

COMMENT LE FICHIER EST ORGANISÉ (déduit d'un replay Y11S3_Alpha04)
    - En-tête "dissect" : paires clé/valeur (joueurs, opérateurs, équipes, carte, score...).
    - Puis des frames zstd. Une fois décompressé, le flux est fait de plusieurs CANAUX écrits
      l'un après l'autre ; chaque canal couvre toute la manche et se découpe en IMAGES (ticks) :
          [u32 numéro d'image][u32 taille][u32 0][données]
      Le numéro d'image est commun à tous les canaux : c'est la base de temps.
    - Les objets du jeu sont des entités identifiées par un u32 (0xF0??xxxx ; le préfixe des joueurs
      change d'une manche à l'autre : 0xF00D, 0xF004... il est détecté automatiquement).
      Leurs positions sont des paquets  <entité><0><champ> 60 73 85 FE <sous-type> <x y z float32>
      (canal principal) ou  <entité><0> <x y z float32> <0>  (canal des positions).
    - L'orientation (lacet) est un vecteur unitaire  <entité><0> <x y 0> <0>  du canal des positions ;
      la caméra (avec le tangage) est un vecteur unitaire 3D indexé par une adresse mémoire.
    - L'horloge de la manche est un paquet  18 37 46 6C 04 <u32 millisecondes restantes>.
    - Le fil d'événements (kills, messages) est un paquet  59 34 E5 8B 04 ...

LIMITES
    - Le replay est enregistré du point de vue d'un joueur : le serveur n'envoie pas toujours la
      position des adversaires (quand ils ne sont pas "pertinents" pour ce joueur). Il y a donc des
      trous dans leurs trajectoires ; le lecteur affiche alors la dernière position connue.
    - Les entités ne sont pas reliées directement aux pseudos dans le flux : la correspondance est
      déduite des morts (une trajectoire s'arrête quand le joueur meurt) et de la proximité du
      tueur. Vérifie-la dans resume.txt et corrige-la avec --noms si besoin.
    - Les signatures d'octets peuvent changer d'une saison du jeu à l'autre.
"""
import argparse, collections, csv, json, math, os, re, struct, sys
import numpy as np

try:
    import zstandard
except ImportError:
    sys.exit("Il manque zstandard :  pip install zstandard")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CLOCK_MS_SIG = b"\x18\x37\x46\x6c\x04"     # + u32 : millisecondes restantes dans la phase
FEED_SIG = b"\x59\x34\xe5\x8b\x04"         # paquet du fil d'événements (kill, message...)
KILLER_TAG = b"\xd9\x13\x3c\xba"           # précédé de 0x22, suivi de <longueur u8><pseudo>
TARGET_TAG = b"\xac\x19\x0f\x70"
MESSAGE_TAG = b"\x26\xe3\x09\x00\x79\x00\x00\x00\x00"   # suivi de <longueur u8><texte>
PROP_SIG = b"\x60\x73\x85\xfe"             # paquet de propriété d'entité (positions dans le canal principal)
PTR_RX = rb"....[\x00-\xff]\x02\x00\x00"                 # adresse mémoire 64 bits 0x0000_02xx_xxxx_xxxx

# --- paramètres ---
MAX_FRAME = 10_000_000  # numéro d'image maximal plausible (≈ 26 h à 100 images/s)
PAWN_MIN_PTS = 300       # une entité "joueur" a au moins ce nombre de positions dans le canal principal
OUTLIER_DXY = 2.0        # m : écart max à la médiane des voisins avant de rejeter un point
OUTLIER_DZ = 2.5
VIEWER_STEP = 0.1        # s : pas d'échantillonnage des positions dans le lecteur HTML

# Identifiants de cartes (source : projet r6-dissect, https://github.com/redraskal/r6-dissect)
MAPS = {837214085: "Clubhouse", 1378191338: "Café Dostoïevski", 1460220617: "Canal", 1767965020: "Yacht",
        2609218856: "Avion présidentiel", 2609221242: "Consulat", 2697268122: "Université Bartlett",
        42090092951: "Littoral", 53627213396: "Tour", 88107330328: "Villa", 126196841359: "Forteresse",
        127951053400: "Base d'Hereford", 199824623654: "Parc d'attractions", 231702797556: "Oregon",
        237873412352: "Maison", 259816839773: "Chalet", 276279025182: "Gratte-ciel", 305979357167: "Frontière",
        329867321446: "Favela", 355496559878: "Banque", 362605108559: "Outback", 365284490964: "Emerald Plains",
        270063334510: "Stade Bravo", 378595635123: "Nighthaven Labs", 379218689149: "Consulat",
        388073319671: "Repaire", 405306299908: "Stade 2020", 413779563590: "Banque", 407987100456: "Frontière",
        407558616688: "Chalet", 407193663917: "Clubhouse", 413845419788: "Café Dostoïevski",
        417890697769: "Repaire", 418119057546: "Nighthaven Labs", 418126004176: "Consulat"}


# ----------------------------------------------------------------------------- lecture
def read_rec(path):
    with open(path, "rb") as f:
        d = f.read()
    if not d.startswith(b"dissect"):
        print("Attention : le fichier ne commence pas par 'dissect', est-ce bien un .rec ?")
    start = d.find(ZSTD_MAGIC)
    if start < 0:
        sys.exit("Aucune frame zstd trouvée dans le fichier.")
    header = d[:start]
    raw = bytearray()
    pos = start
    while 0 <= pos < len(d):
        if d[pos:pos + 4] != ZSTD_MAGIC:
            pos = d.find(ZSTD_MAGIC, pos)
            continue
        do = zstandard.ZstdDecompressor().decompressobj()
        try:
            raw += do.decompress(d[pos:])
        except Exception:
            pos += 4
            continue
        pos = len(d) - len(do.unused_data)
    return header, bytes(raw)


def read_header(header):
    """En-tête : suite de <u64 longueur><clé><u64 longueur><valeur>. Renvoie (méta, joueurs)."""
    i = header.find(b"playerid") - 8
    meta, players, cur = {}, [], None
    while 0 <= i and i + 8 <= len(header):
        n = struct.unpack_from("<Q", header, i)[0]
        if not 0 < n < 100:
            break
        k = header[i + 8:i + 8 + n].decode("latin1")
        i += 8 + n
        m = struct.unpack_from("<Q", header, i)[0]
        if m > 10_000:
            break
        v = header[i + 8:i + 8 + m].decode("utf-8", "replace")
        i += 8 + m
        if k == "playerid":
            cur = {"id": v}
            players.append(cur)
        elif k in ("playername", "team", "rolename") and cur is not None:
            cur[k] = v
        else:
            cur = None if k in ("version", "datetime") else cur
            meta.setdefault(k, v)
    seen, out = set(), []
    for p in players:                     # l'en-tête répète parfois un bloc
        if p.get("playername") and p["id"] not in seen:
            seen.add(p["id"])
            out.append({"pseudo": p["playername"], "id": p["id"], "operateur": p.get("rolename", "?"),
                        "equipe": p.get("team", "?")})
    return meta, out


# ----------------------------------------------------------------------------- images (ticks)
def split_frames(raw, min_frames=20):
    """Découpe le flux en canaux d'images [u32 numéro][u32 taille][u32 0][données].
    Renvoie deux tableaux triés : début des données de chaque image, numéro de l'image."""
    starts, numbers = [], []
    pos, n = 0, len(raw)
    while pos < n - 12:
        fn, sz, z = struct.unpack_from("<III", raw, pos)
        if z == 0 and sz <= 1_000_000 and fn < MAX_FRAME:
            chain, p = [], pos
            while p + 12 <= n:
                f2, s2, z2 = struct.unpack_from("<III", raw, p)
                if z2 != 0 or s2 > 1_000_000 or p + 12 + s2 > n:
                    break
                chain.append((p + 12, f2))
                p += 12 + s2
            nums = np.array([c[1] for c in chain])
            # un vrai canal : assez d'images, numérotées dans l'ordre
            if len(chain) >= min_frames and (np.diff(nums) >= 0).mean() > 0.9:
                for s, f in chain:
                    if f < MAX_FRAME:
                        starts.append(s)
                        numbers.append(f)
                pos = p
                continue
        pos += 1
    if not starts:
        sys.exit("Impossible de découper le flux en images : format inconnu pour ce replay.")
    return np.array(starts), np.array(numbers)


class FrameIndex:
    def __init__(self, raw):
        self.starts, self.numbers = split_frames(raw)

    def frame(self, offsets):
        """Numéro d'image contenant chaque offset du flux."""
        i = np.searchsorted(self.starts, np.asarray(offsets), side="right") - 1
        return self.numbers[np.clip(i, 0, None)]


# ----------------------------------------------------------------------------- horloge
class Clock:
    """Convertit un numéro d'image en temps du replay (s depuis le début de la préparation),
    en phase de jeu et en chrono affiché (temps restant)."""

    def __init__(self, raw, fi):
        offs = [m.start() for m in re.finditer(re.escape(CLOCK_MS_SIG), raw)]
        if not offs:
            sys.exit("Aucune horloge de manche trouvée : les signatures ne correspondent sans doute pas à ce replay.")
        ms = [struct.unpack_from("<I", raw, o + 5)[0] if o + 9 <= len(raw) else 1 << 31 for o in offs]
        samples = sorted(set((int(f), v) for f, v in zip(fi.frame(offs), ms) if v <= 3_600_000))
        zeros = [f for f, v in samples if v == 0]
        samples = [s for s in samples if s[1] > 0]        # 0 = phase terminée ou manche finie : traité à part
        if not samples:
            sys.exit("Horloge de manche vide.")
        frames = np.array([s[0] for s in samples])
        vals = np.array([s[1] for s in samples], dtype=float)
        # vitesse moyenne (s par image) sur les paires qui décroissent normalement
        df, dv = np.diff(frames), -np.diff(vals)
        ok = (df > 0) & (dv > 0) & (dv < df * 50)
        self.rate = float(np.median(dv[ok] / df[ok]) / 1000) if ok.any() else 0.0096
        # phases : l'horloge remonte -> nouvelle phase
        t, phase = [0.0], [0]
        self.phase_len = [math.ceil(vals[0] / 1000)]
        t[0] = self.phase_len[0] - vals[0] / 1000
        for i in range(1, len(frames)):
            gap = (frames[i] - frames[i - 1]) * self.rate
            if vals[i] > vals[i - 1] + 1000:
                phase.append(phase[-1] + 1)
                self.phase_len.append(math.ceil(vals[i] / 1000))
                t.append(t[-1] + gap)
            else:
                phase.append(phase[-1])
                t.append(t[-1] + max((vals[i - 1] - vals[i]) / 1000, 0.0))
        # fin de manche : l'horloge retombe à 0 alors qu'il restait du temps
        self.end_frame = None
        after = [f for f in zeros if f > frames[-1]]
        if after and vals[-1] > 1000:
            self.end_frame = int(min(after))
        self.frames, self.t, self.vals, self.phase = frames, np.array(t), vals, np.array(phase)
        n_ph = self.phase.max() + 1
        self.action_frame = int(frames[np.argmax(self.phase == 1)]) if n_ph > 1 else int(frames[0])

    def time(self, f):
        f = np.asarray(f, dtype=float)
        t = np.interp(f, self.frames, self.t)
        t = np.where(f < self.frames[0], self.t[0] + (f - self.frames[0]) * self.rate, t)
        return np.where(f > self.frames[-1], self.t[-1] + (f - self.frames[-1]) * self.rate, t)

    def phase_name(self, ph):
        if ph == 0:
            return "préparation"
        if ph == 1:
            return "action"
        return "désamorceur posé" if self.phase_len[ph] <= 60 else f"phase {ph}"

    def chrono(self, f):
        """(phase, temps restant affiché 'm:ss') pour une image."""
        i = max(int(np.searchsorted(self.frames, f, side="right")) - 1, 0)
        ph = int(self.phase[i])
        if self.end_frame is not None and f >= self.end_frame:
            return "fin de manche", "0:00"
        rem = max(self.vals[i] / 1000 - (f - self.frames[i]) * self.rate, 0)
        s = math.ceil(rem)
        return self.phase_name(ph), f"{s // 60}:{s % 60:02d}"


# ----------------------------------------------------------------------------- événements
def read_events(raw, fi, clock):
    """Kills et messages du fil d'événements, dédoublonnés (le jeu renvoie plusieurs fois le même)."""
    out, seen = [], set()
    offs = [m.start() for m in re.finditer(re.escape(FEED_SIG), raw)]
    for o, f in zip(offs, fi.frame(offs)):
        win = raw[o:o + 700]

        def sread(tag, prefix=b"\x22"):
            i = win.find(prefix + tag)
            if i < 0:
                return ""
            j = i + len(prefix) + len(tag)
            if j >= len(win):
                return ""
            n = win[j]
            return win[j + 1:j + 1 + n].decode("utf-8", "replace") if 0 < n < 120 else ""
        killer, target = sread(KILLER_TAG), sread(TARGET_TAG)
        text = sread(MESSAGE_TAG, b"")
        if killer and target:
            key = ("kill", killer, target)
            ev = {"type": "kill", "tueur": killer, "victime": target, "texte": f"{killer} a tué {target}"}
        elif len(text) >= 4:
            key = ("message", text)
            ev = {"type": "message", "tueur": "", "victime": "", "texte": text}
        else:
            continue
        if key in seen or f >= 1 << 31:
            continue
        seen.add(key)
        ev["frame"] = int(f)
        out.append(ev)
    if clock.end_frame is not None:
        out.append({"type": "fin", "tueur": "", "victime": "", "texte": "Fin de la manche", "frame": clock.end_frame})
    out.sort(key=lambda e: e["frame"])
    for e in out:
        e["t"] = round(float(clock.time(e["frame"])), 2)
        e["phase"], e["chrono"] = clock.chrono(e["frame"])
    return out


# ----------------------------------------------------------------------------- positions
def plausible(x, y, z):
    return (np.isfinite(x) & np.isfinite(y) & np.isfinite(z) & (np.abs(x) < 1000) & (np.abs(y) < 1000)
            & (z > -50) & (z < 200) & ~((np.abs(y) < 1e-6) & (np.abs(z) < 1e-6))
            & ((np.abs(x) > 1.5) | (np.abs(y) > 1.5)))


def entity_prefix(raw, _cache={}):
    """Octets hauts (2 octets) des identifiants d'entité des joueurs, ex. b"\\x0d\\xf0" pour 0xF00Dxxxx.
    Ils changent d'une manche à l'autre : on prend le préfixe le plus fréquent des paquets de propriété."""
    if id(raw) not in _cache:
        c = collections.Counter(raw[m.start() - 10:m.start() - 8] for m in re.finditer(re.escape(PROP_SIG), raw)
                                if m.start() >= 12 and raw[m.start() - 8:m.start() - 4] == b"\0\0\0\0"
                                and raw[m.start() - 9] == 0xF0)
        if not c:
            sys.exit("Aucun paquet de propriété d'entité : les signatures ne correspondent sans doute pas à ce replay.")
        _cache[id(raw)] = c.most_common(1)[0][0]
    return _cache[id(raw)]


def entity_rx(raw):
    """Motif d'un identifiant d'entité joueur suivi de 4 octets nuls."""
    return b"(..)" + re.escape(entity_prefix(raw)) + b"\x00\x00\x00\x00"


def read_positions(raw, fi):
    """Positions de toutes les entités : {entité: [(image, x, y, z, source)]}.
    source 0 = paquet de propriété (canal principal), 1 = canal des positions (entité),
    2 = canal des positions (adresse mémoire rattachée au joueur)."""
    pts = collections.defaultdict(list)
    prop_count = collections.Counter()
    # paquets de propriété : <entité><0><champ> PROP_SIG <sous-type u16> <x y z>
    offs, prefix = [], entity_prefix(raw)
    for m in re.finditer(re.escape(PROP_SIG), raw):
        o = m.start()
        if o < 12 or raw[o - 10:o - 4] != prefix + b"\0\0\0\0" or o + 18 > len(raw):
            continue
        if raw[o + 4] & 0xF0 not in (0xB0, 0xC0, 0xE0):
            continue
        offs.append(o)
    if offs:
        a = np.array(offs)
        ent = np.array([struct.unpack_from("<I", raw, o - 12)[0] for o in offs])
        sub = np.array([raw[o + 4] & 0xF0 for o in offs])
        xyz = np.array([struct.unpack_from("<3f", raw, o + 6) for o in offs])
        ok = plausible(xyz[:, 0], xyz[:, 1], xyz[:, 2])
        fr = fi.frame(a)
        for e, s, f, p, k in zip(ent, sub, fr, xyz, ok):
            if k:
                pts[int(e)].append((int(f), p[0], p[1], p[2], 0))
                if s == 0xB0:
                    prop_count[int(e)] += 1
    # canal des positions : <entité><0> <x y z> <u32 0>
    offs = [m.start() for m in re.finditer(entity_rx(raw), raw, re.S)]
    offs = [o for o in offs if o + 24 <= len(raw)]
    if offs:
        a = np.array(offs)
        vals = np.array([struct.unpack_from("<I3fI", raw, o) for o in offs])
        xyz = vals[:, 1:4]
        ok = plausible(xyz[:, 0], xyz[:, 1], xyz[:, 2]) & (vals[:, 4] == 0)
        fr = fi.frame(a)
        for e, f, p, k in zip(vals[ok, 0], fr[ok], xyz[ok], ok[ok]):
            pts[int(e)].append((int(f), p[0], p[1], p[2], 1))
    pawns = [e for e, c in prop_count.items() if c >= PAWN_MIN_PTS]
    tracks = {e: clean_track(np.array(sorted(pts[e]))) for e in pawns}
    return {e: fill_gaps(t, pointer_tracks(raw, fi)) for e, t in tracks.items()} if tracks else tracks


def pointer_tracks(raw, fi, _cache={}):
    """Canal des positions : certains objets y sont indexés par une adresse mémoire 64 bits
    (0x0000_02xx_xxxx_xxxx) au lieu d'un identifiant d'entité. Renvoie {adresse: [(image, x, y, z, 2)]}."""
    if id(raw) in _cache:
        return _cache[id(raw)]
    offs = [m.start() for m in re.finditer(PTR_RX, raw, re.S)]
    offs = [o for o in offs if o + 24 <= len(raw)]
    by_key = collections.defaultdict(list)
    if offs:
        keys = np.array([struct.unpack_from("<Q", raw, o)[0] for o in offs])
        xyz = np.array([struct.unpack_from("<3f", raw, o + 8) for o in offs])
        tail0 = np.array([raw[o + 20:o + 24] == b"\0\0\0\0" for o in offs])
        ok = plausible(xyz[:, 0], xyz[:, 1], xyz[:, 2]) & tail0
        fr = fi.frame(np.array(offs))
        for k, f, p in zip(keys[ok], fr[ok], xyz[ok]):
            by_key[int(k)].append((int(f), p[0], p[1], p[2], 2))
    out = {k: np.array(sorted(v)) for k, v in by_key.items() if len(v) >= 100}
    _cache[id(raw)] = out
    return out


def fill_gaps(track, candidates, min_gap=100, max_link=2.0):
    """Comble les trous d'une trajectoire avec les points d'une adresse mémoire, seulement si ces points
    se raccordent aux deux bords du trou (à moins de max_link m) et restent continus entre eux.
    Les adresses mémoire sont réutilisées par d'autres objets : sans ces garde-fous on mélange tout."""
    if len(track) < 2:
        return track
    added = []
    gaps = np.where(np.diff(track[:, 0]) > min_gap)[0]
    for g in gaps:
        a, b = track[g], track[g + 1]
        best = None
        for pts in candidates.values():
            inside = pts[(pts[:, 0] > a[0]) & (pts[:, 0] < b[0])]
            if len(inside) < 3:
                continue
            steps = np.hypot(*np.diff(inside[:, 1:3], axis=0).T)
            if (math.hypot(*(inside[0, 1:3] - a[1:3])) <= max_link and math.hypot(*(inside[-1, 1:3] - b[1:3])) <= max_link
                    and abs(inside[0, 3] - a[3]) <= OUTLIER_DZ and abs(inside[-1, 3] - b[3]) <= OUTLIER_DZ
                    and (len(steps) == 0 or steps.max() <= max_link)):
                if best is None or len(inside) > len(best):
                    best = inside
        if best is not None:
            added.append(best)
    if not added:
        return track
    t = np.vstack([track] + added)
    return t[np.argsort(t[:, 0], kind="stable")]


def clean_track(a):
    """Retire les doublons et les points isolés (écart à la médiane des voisins trop grand)."""
    if len(a) < 7:
        return a
    _, idx = np.unique(np.round(a[:, :4], 3), axis=0, return_index=True)
    a = a[np.sort(idx)]
    a = a[np.argsort(a[:, 0], kind="stable")]
    for _ in range(2):
        n = len(a)
        keep = np.ones(n, bool)
        for i in range(n):
            lo, hi = max(0, i - 4), min(n, i + 5)
            nb = np.delete(a[lo:hi, 1:4], i - lo, axis=0)
            med = np.median(nb, axis=0)
            if math.hypot(*(a[i, 1:3] - med[:2])) > OUTLIER_DXY or abs(a[i, 3] - med[2]) > OUTLIER_DZ:
                keep[i] = False
        a = a[keep]
    return a


def read_facing(raw, fi, tracks):
    """Orientation des joueurs : {entité: tableau (image, lacet°, tangage°)}.
    - lacet (direction horizontale) : vecteur unitaire <entité><0> <x y 0> <0> du canal des positions ;
    - tangage (regard vers le haut/bas) : vecteur unitaire 3D de la caméra, indexé par une adresse mémoire.
      Une caméra est rattachée au joueur dont le lacet colle à sa direction horizontale ; on ne garde
      un échantillon que s'il reste proche du lacet du corps (< 45°), car les adresses mémoire
      sont réutilisées par d'autres objets.
    Angles en degrés ; lacet = atan2(y, x), dans le repère de la carte. Tangage NaN si inconnu."""
    yaw = collections.defaultdict(list)
    for m in re.finditer(entity_rx(raw), raw, re.S):
        o = m.start()
        if o + 24 > len(raw):
            continue
        e = struct.unpack_from("<I", raw, o)[0]
        if e not in tracks:
            continue
        x, y, z, w = struct.unpack_from("<3fI", raw, o + 8)
        if w == 0 and z == 0 and math.isfinite(x) and math.isfinite(y) and abs(math.hypot(x, y) - 1) < 0.01:
            yaw[e].append((o, math.degrees(math.atan2(y, x))))
    out = {}
    for e, v in yaw.items():
        fr = fi.frame([o for o, _ in v])
        a = np.array([(f, d, np.nan) for f, (_, d) in zip(fr, v)])
        out[e] = a[np.argsort(a[:, 0], kind="stable")]

    # caméras : adresse mémoire portant à la fois des positions (qui collent au joueur) et des directions 3D
    cams = collections.defaultdict(list)
    for m in re.finditer(PTR_RX, raw, re.S):
        o = m.start()
        if o + 24 > len(raw):
            continue
        x, y, z, w = struct.unpack_from("<3fI", raw, o + 8)
        if w == 0 and math.isfinite(x) and math.isfinite(y) and math.isfinite(z) \
                and abs(math.sqrt(x * x + y * y + z * z) - 1) < 0.01 and abs(z) < 0.999:
            cams[struct.unpack_from("<Q", raw, o)[0]].append((o, x, y, z))
    # chaque caméra est rattachée au joueur dont le lacet du corps colle à sa direction horizontale
    # (écart < 10° dans au moins 40 % des cas, et au moins 3 fois plus souvent qu'avec n'importe qui d'autre)
    body = {e: a for e, a in out.items()}
    rows = collections.defaultdict(list)
    for k, v in cams.items():
        if len(v) < 100 or np.mean([abs(q[3]) > 0.01 for q in v]) < 0.8:
            continue
        fr = fi.frame([q[0] for q in v])
        yw = np.degrees(np.arctan2([q[2] for q in v], [q[1] for q in v]))
        scores = {}
        for e, b in body.items():
            idx = np.arange(0, len(v), max(1, len(v) // 200))
            j = np.clip(np.searchsorted(b[:, 0], fr[idx]), 1, len(b) - 1)
            j = np.where(np.abs(b[j, 0] - fr[idx]) < np.abs(b[j - 1, 0] - fr[idx]), j, j - 1)
            near = np.abs(b[j, 0] - fr[idx]) <= 20
            if near.sum() >= 30:
                diff = np.abs((yw[idx] - b[j, 1] + 180) % 360 - 180)
                scores[e] = float((diff[near] < 10).mean())
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        e, s = ranked[0]
        if s < 0.4 or (len(ranked) > 1 and s < 3 * ranked[1][1]):
            continue
        b = body[e]
        for f, (_, x, y, z), w in zip(fr, v, yw):
            i = int(np.argmin(np.abs(b[:, 0] - f)))
            if abs(b[i, 0] - f) <= 50 and abs((w - b[i, 1] + 180) % 360 - 180) < 45:
                rows[e].append((f, w, math.degrees(math.asin(max(-1.0, min(1.0, z))))))
    for e, r in rows.items():
        a = np.vstack([out[e], np.array(r)])
        out[e] = a[np.argsort(a[:, 0], kind="stable")]
    return out


def resample_facing(facing, clock, t_min, t_max):
    """Orientation pour le lecteur : [t, lacet, tangage|None], une valeur tous les VIEWER_STEP s au plus.
    Le tangage est reporté depuis le dernier échantillon de caméra (moins de 1 s avant)."""
    if facing is None or len(facing) == 0:
        return []
    t = clock.time(facing[:, 0])
    out, last, last_pitch = [], -1e9, (None, -1e9)
    for (f, yw, pt), tt in zip(facing, t):
        if not t_min - 1 <= tt <= t_max:
            continue
        if not math.isnan(pt):
            last_pitch = (round(float(pt)), tt)
        if tt - last >= VIEWER_STEP:
            pitch = last_pitch[0] if tt - last_pitch[1] <= 1.0 else None
            out.append([round(float(tt), 2), round(float(yw), 1), pitch])
            last = tt
    return out


def position_at(track, f, max_gap=200):
    i = int(np.argmin(np.abs(track[:, 0] - f)))
    return track[i, 1:4] if abs(track[i, 0] - f) <= max_gap else None


# ----------------------------------------------------------------------------- qui est qui
def identify(tracks, players, events, clock, facing=None, recorder=None):
    """Associe chaque entité à un joueur de l'en-tête.
    1. défense = entités présentes pendant la préparation, attaque = celles qui apparaissent ensuite ;
    2. une victime = la trajectoire qui s'arrête juste après son kill ;
    3. le joueur qui a enregistré le replay (s'il survit) = la première entité de son équipe à apparaître :
       son propre jeu connaît son personnage avant ceux des autres (vérifié sur deux manches) ;
    4. un autre survivant = l'entité la plus proche de ses victimes et tournée vers elles au moment des kills,
       sinon par élimination ; l'attribution est marquée INCERTAIN quand deux entités sont presque à égalité.
    Renvoie {entité: (joueur, méthode)} et {équipe: 'défense'/'attaque'}."""
    # les attaquants peuvent apparaître dans le flux un peu avant la fin de la préparation : marge de 3 s
    early = [e for e, t in tracks.items() if t[0, 0] < clock.action_frame - 3.0 / clock.rate]
    late = [e for e in tracks if e not in early]
    last = {e: t[-1, 0] for e, t in tracks.items()}
    kills = [e for e in events if e["type"] == "kill"]
    death = {}
    for k in kills:
        death.setdefault(k["victime"], k["frame"])

    def match_victims(group, members):
        res, cost, free = {}, 0.0, list(group)
        for p in sorted([p for p in members if p["pseudo"] in death], key=lambda p: death[p["pseudo"]]):
            if not free:
                break
            f = death[p["pseudo"]]
            cand = [e for e in free if last[e] >= f - 100] or free
            e = min(cand, key=lambda e: abs(last[e] - f))
            res[e] = (p, "mort")
            cost += abs(last[e] - f)
            free.remove(e)
        return res, cost

    teams = sorted(set(p["equipe"] for p in players))
    if len(teams) != 2:
        return {}, {}
    best = None
    for defense in teams:
        attack = teams[1 - teams.index(defense)]
        mem = {defense: [p for p in players if p["equipe"] == defense], attack: [p for p in players if p["equipe"] == attack]}
        r1, c1 = match_victims(early, mem[defense])
        r2, c2 = match_victims(late, mem[attack])
        penalty = abs(len(early) - len(mem[defense])) + abs(len(late) - len(mem[attack]))
        if best is None or (penalty, c1 + c2) < best[0]:
            best = ((penalty, c1 + c2), defense, attack, mem, {**r1, **r2})
    _, defense, attack, mem, res = best
    victim_ent = {p["pseudo"]: e for e, (p, _) in res.items()}

    def last_pos(tr, f):
        a = tr[(tr[:, 0] <= f) & (tr[:, 0] > f - 300)]
        return a[-1, 1:4] if len(a) else None

    for team, group in ((defense, early), (attack, late)):
        free = [e for e in group if e not in res]
        survivors = [p for p in mem[team] if p["pseudo"] not in death]
        rec = next((p for p in survivors if p["pseudo"] == recorder), None)
        if rec is not None and free:
            first = sorted(free, key=lambda e: tracks[e][0, 0])
            gap = (tracks[first[1]][0, 0] - tracks[first[0]][0, 0]) * clock.rate if len(first) > 1 else 1e9
            if gap >= 0.5:
                res[first[0]] = (rec, "joueur qui enregistre : premier de son équipe à apparaître")
                free.remove(first[0])
                survivors = [p for p in survivors if p is not rec]
        # score d'un survivant pour une entité : proche de ses victimes, et tourné vers elles, au moment des kills
        score = collections.defaultdict(float)
        for k in kills:
            v = victim_ent.get(k["victime"])
            vpos = last_pos(tracks[v], k["frame"]) if v is not None else None
            if vpos is None:
                continue
            for p in survivors:
                if k["tueur"] != p["pseudo"]:
                    continue
                for e in free:
                    q = last_pos(tracks[e], k["frame"])
                    if q is None:
                        continue
                    score[(e, p["pseudo"])] += 1.0 / (1.0 + float(np.linalg.norm(q[:2] - vpos[:2])) / 5)
                    fa = facing.get(e) if facing else None
                    if fa is not None:
                        recent = fa[(fa[:, 0] <= k["frame"]) & (fa[:, 0] > k["frame"] - 100)]
                        if len(recent):
                            bearing = math.degrees(math.atan2(vpos[1] - q[1], vpos[0] - q[0]))
                            err = abs((recent[-1, 1] - bearing + 180) % 360 - 180)
                            score[(e, p["pseudo"])] += max(0.0, math.cos(math.radians(err)))
        taken, doubtful = set(), False
        for (e, name), s in sorted(score.items(), key=lambda kv: -kv[1]):
            if e in free and name not in taken:
                others = [s2 for (e2, n2), s2 in score.items() if n2 == name and e2 != e and e2 in free]
                how = "proximité et visée vers ses victimes"
                if others and s < 1.5 * max(others):
                    alt = [e2 for (e2, n2), s2 in score.items() if n2 == name and e2 != e and e2 in free and s2 == max(others)]
                    how += f" (INCERTAIN : pourrait être {hex(alt[0])})"
                    doubtful = True
                res[e] = (next(p for p in survivors if p["pseudo"] == name), how)
                free.remove(e)
                taken.add(name)
        rest = [p for p in survivors if p["pseudo"] not in taken]
        how = "élimination" if len(free) == 1 and len(rest) == 1 else "au hasard (à vérifier)"
        if doubtful and how == "élimination":
            how += " (INCERTAIN, dépend de l'attribution ci-dessus)"
        for e, p in zip(free, rest):
            res[e] = (p, how)
    return res, {defense: "défense", attack: "attaque"}


# ----------------------------------------------------------------------------- étages
def floors(z_all, bin_m=0.25, min_share=0.03, min_sep=2.5):
    """Découpe les hauteurs en niveaux : un niveau = un pic de l'histogramme des z (>= 3 % des points),
    deux niveaux sont séparés d'au moins 2,5 m ; la coupure passe au creux entre deux pics.
    Renvoie [(z_min, z_max, z_typique)] du plus bas au plus haut."""
    z = np.asarray(z_all, dtype=float)
    if len(z) == 0:
        return []
    edges = np.arange(math.floor(z.min()) - bin_m, math.ceil(z.max()) + 2 * bin_m, bin_m)
    h, _ = np.histogram(z, bins=edges)
    centers = (edges[:-1] + edges[1:]) / 2
    peaks = [i for i in range(len(h)) if h[i] >= min_share * len(z)
             and h[i] >= h[max(i - 1, 0)] and h[i] >= h[min(i + 1, len(h) - 1)]]
    kept = []
    for i in sorted(peaks, key=lambda i: -h[i]):            # les pics les plus forts d'abord
        if all(abs(centers[i] - centers[j]) >= min_sep for j in kept):
            kept.append(i)
    kept.sort()
    cuts = [float(centers[i + int(np.argmin(h[i:j + 1]))]) for i, j in zip(kept, kept[1:])]
    bounds = [-1e9] + cuts + [1e9]
    return [(lo, hi, round(float(centers[k]), 1)) for lo, hi, k in zip(bounds, bounds[1:], kept)]


# ----------------------------------------------------------------------------- rendu
DEF_COLORS = ["#1F77B4", "#17BECF", "#2CA02C", "#4C6EF5", "#0B7285"]
ATT_COLORS = ["#D62728", "#FF7F0E", "#E377C2", "#B8860B", "#8C564B"]


def render_png(entries, outdir, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    bg, ink = "#FFFFFF", "#222222"
    halo = [pe.withStroke(linewidth=3, foreground=bg)]
    fig, ax = plt.subplots(figsize=(12, 9.5), facecolor=bg)
    for p in entries:
        a = p["track"]
        ax.plot(a[:, 1], a[:, 2], ".", ms=1.5, color=p["couleur"], zorder=3,
                label=f"{p['pseudo']} ({p['operateur']}, {p['role']})")
        ax.plot(a[0, 1], a[0, 2], "o", ms=8, color=p["couleur"], mec=bg, mew=1.5, zorder=4)
        ax.plot(a[-1, 1], a[-1, 2], "X" if p["mort"] is not None else "s", ms=9, color=p["couleur"], mec=bg, mew=1.5, zorder=4)
        ax.text(a[0, 1], a[0, 2], f" {p['pseudo']}", fontsize=9, fontweight="bold", color=ink, path_effects=halo, zorder=6)
    ax.set_aspect("equal"); ax.grid(color="#EEEEEE", lw=0.6); ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"{title} · rond = départ · croix = mort · carré = fin", fontsize=11, color=ink, loc="left")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9)
    plt.tight_layout(); plt.savefig(os.path.join(outdir, "trajectoires.png"), dpi=110, facecolor=bg); plt.close()


def resample(track, clock, death_frame):
    """Positions pour le lecteur : une tous les VIEWER_STEP s au plus, temps en s.
    Un trou qui commence et finit au même endroit (< 0,75 m) = joueur immobile (le jeu n'envoie rien
    quand rien ne change) : on le remplit avec la même position pour ne pas l'afficher comme inconnu."""
    t = clock.time(track[:, 0])
    out, last = [], -1e9
    for (f, x, y, z, _), tt in zip(track, t):
        if death_frame is not None and f > death_frame + 150:
            break
        if out and tt - out[-1][0] > 1.0 and math.hypot(x - out[-1][1], y - out[-1][2]) < 0.75 and abs(z - out[-1][3]) < 0.5:
            px, py, pz = out[-1][1:]
            k = out[-1][0] + 0.5
            while k < tt - 0.25:
                out.append([round(k, 2), px, py, pz])
                k += 0.5
        if tt - last >= VIEWER_STEP:
            out.append([round(float(tt), 2), round(float(x), 2), round(float(y), 2), round(float(z), 2)])
            last = tt
    return out


def render_html(entries, events, clock, meta, fl, outdir, title):
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "r6_viewer.html")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    phases = []
    for ph in sorted(set(clock.phase.tolist())):
        fr = clock.frames[clock.phase == ph]
        phases.append({"nom": clock.phase_name(ph), "debut": round(float(clock.time(fr[0])), 2),
                       "duree": clock.phase_len[ph]})
    data = {
        "titre": title,
        "joueurs": [{"pseudo": p["pseudo"], "operateur": p["operateur"], "role": p["role"], "couleur": p["couleur"],
                     "entite": p["entite"], "mort": p["mort"], "points": p["points"], "vue": p["vue"]} for p in entries],
        "evenements": [{k: e[k] for k in ("t", "type", "texte", "phase", "chrono", "tueur", "victime")} for e in events],
        "phases": phases,
        "etages": fl,
        "fin": round(float(clock.time(clock.end_frame if clock.end_frame else clock.frames[-1])), 2),
        "chrono": {"t": [round(float(x), 3) for x in clock.t], "restant": [int(v) for v in clock.vals],
                   "phase": [clock.phase_name(int(p)) for p in clock.phase]},
    }
    html = tpl.replace("__TITRE__", title).replace("/*__DONNEES__*/null", json.dumps(data, ensure_ascii=False))
    with open(os.path.join(outdir, "manche.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Reconstitution d'une manche de R6 Siege à partir d'un replay (.rec)")
    ap.add_argument("rec")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--noms", default="",
                    help='corrige la correspondance entité -> joueur, ex. "0xf00d20d7=Decoy_99,0xf00d209b=nitramossur1" '
                         "(les entités sont listées dans resume.txt)")
    a = ap.parse_args()

    name = os.path.splitext(os.path.basename(a.rec))[0]
    outdir = a.out or f"sortie_{name}"
    os.makedirs(outdir, exist_ok=True)
    log = []

    print("Lecture et décompression...")
    header, raw = read_rec(a.rec)
    meta, players = read_header(header)
    world = int(meta.get("worldid", "0") or 0)
    carte = MAPS.get(world, f"carte inconnue (worldid {world})")
    title = f"{carte} · manche {int(meta.get('roundnumber', '-1')) + 1 if meta.get('roundnumber', '').isdigit() else '?'}"
    log.append(f"Replay : {a.rec}  ·  flux décompressé : {len(raw):,} octets")
    log.append(f"Version {meta.get('version', '?')} · {meta.get('datetime', '?')} · {carte} · "
               f"manche {meta.get('roundnumber', '?')} (numérotée à partir de 0) · "
               f"score avant {meta.get('startingteamscore0', '?')}-{meta.get('startingteamscore1', '?')}, "
               f"après {meta.get('teamscore0', '?')}-{meta.get('teamscore1', '?')}")
    rec_player = next((p["pseudo"] for p in players if p["id"] == meta.get("recordingplayerid")), None)

    print("Découpage en images et lecture de l'horloge...")
    fi = FrameIndex(raw)
    clock = Clock(raw, fi)
    log.append(f"Images : {len(fi.numbers):,} (numéros {fi.numbers.min()} à {fi.numbers.max()}), "
               f"{clock.rate * 1000:.2f} ms par image")
    for ph in sorted(set(clock.phase.tolist())):
        fr = clock.frames[clock.phase == ph]
        log.append(f"  phase {clock.phase_name(ph):18s} : images {fr[0]}-{fr[-1]}, t = {float(clock.time(fr[0])):.1f} s, "
                   f"durée affichée {clock.phase_len[ph]} s")
    if clock.end_frame:
        log.append(f"  fin de manche à l'image {clock.end_frame} (t = {float(clock.time(clock.end_frame)):.1f} s)")

    events = read_events(raw, fi, clock)
    log.append(f"Événements : {len(events)}")
    for e in events:
        log.append(f"  t={e['t']:6.1f} s  [{e['phase']} {e['chrono']}]  {e['texte']}")

    print("Extraction des positions...")
    tracks = read_positions(raw, fi)
    if not tracks:
        sys.exit("Aucune trajectoire de joueur trouvée.")
    print("Extraction des orientations...")
    facing = read_facing(raw, fi, tracks)
    ident, roles = identify(tracks, players, events, clock, facing, rec_player)
    for part in filter(None, (x.strip() for x in a.noms.split(","))):   # corrections manuelles
        if "=" in part:
            k, v = (s.strip() for s in part.split("=", 1))
            try:
                e = int(k, 16)
            except ValueError:
                continue
            p = next((p for p in players if p["pseudo"] == v), {"pseudo": v, "operateur": "?", "equipe": "?"})
            for e2, (p2, _) in list(ident.items()):
                if p2["pseudo"] == v and e2 != e:
                    del ident[e2]
            ident[e] = (p, "--noms")

    death = {}
    for e in events:
        if e["type"] == "kill":
            death.setdefault(e["victime"], e["frame"])
    entries, n_def, n_att = [], 0, 0
    order = sorted(tracks, key=lambda e: (roles.get(ident[e][0]["equipe"], "?") if e in ident else "?",
                                          ident[e][0]["pseudo"].lower() if e in ident else hex(e)))
    for e in order:
        p, how = ident.get(e, ({"pseudo": hex(e), "operateur": "?", "equipe": "?"}, "non identifié"))
        role = roles.get(p["equipe"], "?")
        if role == "défense":
            col = DEF_COLORS[n_def % len(DEF_COLORS)]; n_def += 1
        else:
            col = ATT_COLORS[n_att % len(ATT_COLORS)]; n_att += 1
        df = death.get(p["pseudo"])
        tr = tracks[e]
        fa = facing.get(e)
        if df is not None:
            tr = tr[tr[:, 0] <= df + 150]
            fa = fa[fa[:, 0] <= df] if fa is not None else None
        t_tr = clock.time(tr[[0, -1], 0])
        entries.append({"pseudo": p["pseudo"], "operateur": p["operateur"], "role": role, "couleur": col,
                        "entite": hex(e), "methode": how, "track": tr, "facing": fa,
                        "mort": round(float(clock.time(df)), 2) if df is not None else None,
                        "points": resample(tr, clock, df),
                        "vue": resample_facing(fa, clock, float(t_tr[0]), float(t_tr[1]))})

    log.append(f"Joueurs ({len(entries)} trajectoires) :" + (f"  enregistré par {rec_player}" if rec_player else ""))
    t_end = float(clock.time(clock.end_frame if clock.end_frame else clock.frames[-1]))
    for p in entries:
        t = clock.time(p["track"][:, 0])
        t_stop = p["mort"] if p["mort"] is not None else t_end
        tp = np.array([q[0] for q in p["points"] if q[0] < t_stop] + [t_stop])
        span = np.clip(np.diff(tp), 0, None)
        covered = float(np.where(span <= 1.0, span, 0).sum())
        duree = max(t_stop - float(tp[0]), 1e-9)
        log.append(f"  {p['pseudo']:16s} {p['operateur']:10s} {p['role']:8s} entité {p['entite']}  "
                   f"(identifié par : {p['methode']})  {len(p['track'])} points, de t={float(t[0]):.1f} s "
                   f"à t={float(t[-1]):.1f} s, position connue ~{100 * covered / duree:.0f} % du temps (immobilité comprise)"
                   + (f", mort à t={p['mort']:.1f} s" if p["mort"] is not None else ""))
        fa = p["facing"]
        if fa is None or not len(fa):
            log.append("      orientation : aucune donnée")
        else:
            n_pitch = int(np.isfinite(fa[:, 2]).sum())
            log.append(f"      orientation : {len(fa) - n_pitch} lacets (corps)"
                       + (f", {n_pitch} directions de caméra avec tangage" if n_pitch else ", pas de caméra trouvée (tangage inconnu)"))
    known = {p["pseudo"] for p in entries}
    missing = [p["pseudo"] for p in players if p["pseudo"] not in known]
    if missing:
        log.append(f"ATTENTION : pas de trajectoire pour {', '.join(missing)}")
    log.append("Les entités sont reliées aux pseudos par déduction (morts, proximité du tueur) : "
               "corrige avec --noms \"0x...=pseudo\" si une attribution est fausse.")

    fl = floors(np.concatenate([p["track"][:, 3] for p in entries]))
    log.append("Niveaux détectés (hauteur z) : " + ", ".join(
        f"niveau {i} (z ≈ {zt}, de {max(lo, -99):.1f} à {min(hi, 999):.1f})" for i, (lo, hi, zt) in enumerate(fl)))

    with open(os.path.join(outdir, "evenements.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["t_replay_s", "phase", "chrono", "image", "type", "tueur", "victime", "texte"])
        for e in events:
            w.writerow([e["t"], e["phase"], e["chrono"], e["frame"], e["type"], e["tueur"], e["victime"], e["texte"]])
    with open(os.path.join(outdir, "orientations.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["pseudo", "entite", "image", "t_replay_s", "lacet_deg", "tangage_deg", "source"])
        for p in entries:
            fa = p["facing"]
            if fa is None:
                continue
            for r, t in zip(fa, clock.time(fa[:, 0])):
                cam = not math.isnan(r[2])
                w.writerow([p["pseudo"], p["entite"], int(r[0]), round(float(t), 2), round(float(r[1]), 1),
                            round(float(r[2]), 1) if cam else "", "caméra" if cam else "corps"])
    with open(os.path.join(outdir, "positions.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["pseudo", "operateur", "role", "entite", "image", "t_replay_s", "x", "y", "z"])
        for p in entries:
            for r, t in zip(p["track"], clock.time(p["track"][:, 0])):
                w.writerow([p["pseudo"], p["operateur"], p["role"], p["entite"], int(r[0]), round(float(t), 2),
                            round(r[1], 2), round(r[2], 2), round(r[3], 2)])

    print("Rendu...")
    render_png(entries, outdir, title)
    render_html(entries, events, clock, meta, fl, outdir, title)
    with open(os.path.join(outdir, "resume.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log) + "\n")
    print("\n".join(log))
    print(f"\nTerminé -> {outdir}/  (ouvre manche.html dans un navigateur)")


if __name__ == "__main__":
    main()
