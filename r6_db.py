#!/usr/bin/env python3
"""
r6_db.py : enregistre des replays Rainbow Six Siege (.rec) dans PostgreSQL, pour les revoir et en
tirer des statistiques. L'analyse de chaque manche est celle de r6_replay_tool.py.

Usage :
    python r6_db.py init                                   crée les tables et les vues
    python r6_db.py import MatchReplay/                    importe tous les .rec d'un dossier (récursif)
    python r6_db.py import Match-...-R02.rec Match-...-R03.rec
    python r6_db.py import Match-...-R02.rec --noms "0xf004c777=nitramossur1" --remplacer
    python r6_db.py parties                                parties enregistrées, avec leur numéro
    python r6_db.py liste --partie 2                       manches de la partie 2
    python r6_db.py voir --partie 2                        lecteurs HTML de toutes les manches + index (dossier partie_2/)
    python r6_db.py voir --partie 2 --manche 5             lecteur HTML d'une manche, depuis la base
    python r6_db.py stats [--partie 2]                     bilan par joueur
    python r6_db.py stats --joueur nitramossur1            détail : opérateurs, cartes, duels

Connexion : variable d'environnement R6_DB ou option --db,
par défaut postgresql://r6:r6@localhost:5432/r6

Dépendances :  pip install -r requirements.txt   (psycopg en plus de celles de r6_replay_tool.py)
"""
import argparse, datetime, hashlib, json, math, os, sys, uuid

try:
    import psycopg
except ImportError:
    sys.exit("Il manque psycopg :  pip install \"psycopg[binary]\"")

import numpy as np
import r6_replay_tool as T

DEFAULT_DB = "postgresql://r6:r6@localhost:5432/r6"
NS = uuid.UUID("6f1e4f0c-5f3b-4b8e-9a51-6d2d4c1b7e10")     # pour les joueurs sans profileid


def connect(url):
    try:
        return psycopg.connect(url)
    except psycopg.OperationalError as err:
        sys.exit(f"Connexion impossible à {url} :\n{err}")


# ----------------------------------------------------------------------------- init
def cmd_init(db, a):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    with db.cursor() as cur:
        cur.execute(sql)
    db.commit()
    print("Base prête (tables et vues créées).")


# ----------------------------------------------------------------------------- import
def profil_of(p):
    try:
        return uuid.UUID(p.get("profil", ""))
    except ValueError:
        return uuid.uuid5(NS, p["pseudo"])


def parse_date(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def distance_travelled(track, max_step=3.0):
    """Distance parcourue dans le plan ; les sauts de plus de max_step m (trous dans les données) sont ignorés."""
    if len(track) < 2:
        return 0.0
    d = np.hypot(np.diff(track[:, 1]), np.diff(track[:, 2]))
    return float(d[d <= max_step].sum())


def find_rec(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.lower().endswith(".rec"):
                        yield os.path.join(root, f)
        else:
            yield p


def import_one(db, path, noms="", replace=False):
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    with db.cursor() as cur:
        cur.execute("SELECT id FROM manche WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        if row and not replace:
            return f"déjà importé (manche {row[0]})"
    R = T.analyse(path, noms, verbose=False)
    meta = R["meta"]
    try:
        partie_id = uuid.UUID(meta["id"])
    except (KeyError, ValueError):
        partie_id = uuid.uuid5(NS, f"{meta.get('code', '')}-{meta.get('starttime', '')}")
    date = parse_date(meta.get("datetime"))
    by_name = {p["pseudo"]: p for p in R["players"]}
    for p in R["entries"]:                                   # les entrées corrigées par --noms gardent leur profil
        if p["pseudo"] in by_name:
            p["profil"] = by_name[p["pseudo"]].get("profil", "")
    prof = {p["pseudo"]: profil_of(p) for p in R["players"]}
    for p in R["entries"]:
        prof.setdefault(p["pseudo"], profil_of(p))
    rec = prof.get(R["rec_player"]) if R["rec_player"] else None
    attaque = next((k for k, v in R["roles"].items() if v == "attaque"), None)

    def num(k):
        v = meta.get(k, "")
        return int(v) if v.isdigit() else None

    with db.cursor() as cur:
        cur.execute("""INSERT INTO partie (id, date_debut, carte, worldid, version, mode, type_partie, equipe0_nom, equipe1_nom, num)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, (SELECT coalesce(max(num), 0) + 1 FROM partie))
                       ON CONFLICT (id) DO UPDATE SET date_debut = LEAST(partie.date_debut, EXCLUDED.date_debut)""",
                    (partie_id, date, R["carte"], R["worldid"], meta.get("version"), meta.get("gamemodeid"),
                     meta.get("matchtype"), meta.get("teamname0"), meta.get("teamname1")))
        for name, pid in prof.items():
            cur.execute("""INSERT INTO joueur (profil, pseudo, vu_le) VALUES (%s, %s, %s)
                           ON CONFLICT (profil) DO UPDATE SET
                             pseudo = CASE WHEN joueur.vu_le IS NULL OR EXCLUDED.vu_le >= joueur.vu_le
                                           THEN EXCLUDED.pseudo ELSE joueur.pseudo END,
                             vu_le = GREATEST(joueur.vu_le, EXCLUDED.vu_le)""", (pid, name, date))
        # une manche déjà présente (même fichier, ou même manche enregistrée par un autre joueur)
        cur.execute("SELECT id, sha256 FROM manche WHERE sha256 = %s OR (partie_id = %s AND numero = %s)",
                    (sha, partie_id, R["numero"]))
        old = cur.fetchone()
        if old:
            if not replace:
                db.rollback()
                return f"manche {R['numero']} de cette partie déjà en base (manche {old[0]}) : --remplacer pour l'écraser"
            cur.execute("DELETE FROM manche WHERE id = %s", (old[0],))
        cur.execute("""INSERT INTO manche (partie_id, numero, date_manche, fichier, sha256, enregistre_par, equipe_attaque,
                         vainqueur, condition, score0_avant, score1_avant, score0_apres, score1_apres, duree_s, titre,
                         phases, horloge, etages, journal)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (partie_id, R["numero"], date, os.path.basename(path), sha, rec, attaque, R["vainqueur"], R["condition"],
                     num("startingteamscore0"), num("startingteamscore1"), num("teamscore0"), num("teamscore1"),
                     R["fin"], R["title"], json.dumps(R["phases"]), json.dumps(R["chrono"]),
                     json.dumps(R["etages"]), "\n".join(R["log"])))
        mid = cur.fetchone()[0]
        cur.execute("SELECT num FROM partie WHERE id = %s", (partie_id,))
        pnum = cur.fetchone()[0]

        team = {p["pseudo"]: p["equipe"] for p in R["players"]}
        allie = lambda e: e["type"] == "kill" and team.get(e["tueur"], "?") == team.get(e["victime"], "!")
        kills = {}
        for e in R["events"]:
            if e["type"] == "kill" and not allie(e):
                kills[e["tueur"]] = kills.get(e["tueur"], 0) + 1
        for p in R["entries"]:
            cur.execute("""INSERT INTO participation (manche_id, profil, pseudo, operateur, equipe, role, entite,
                             identification, couleur, mort_s, kills, distance_m)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (mid, prof[p["pseudo"]], p["pseudo"], p["operateur"], p["equipe"], p["role"], p["entite"],
                         p["methode"], p["couleur"], p["mort"], kills.get(p["pseudo"], 0),
                         round(distance_travelled(p["track"]), 1)))
        tracks = {p["pseudo"]: p["track"] for p in R["entries"]}
        for e in R["events"]:
            kp = vp = None
            if e["type"] == "kill":
                if e["tueur"] in tracks:
                    kp = T.position_at(tracks[e["tueur"]], e["frame"], 300)
                if e["victime"] in tracks:
                    vp = T.position_at(tracks[e["victime"]], e["frame"], 300)
            dist = float(np.linalg.norm(kp - vp)) if kp is not None and vp is not None else None
            cur.execute("""INSERT INTO evenement (manche_id, t_s, image, phase, chrono, type, tueur, victime, texte, allie,
                             tueur_x, tueur_y, tueur_z, victime_x, victime_y, victime_z, distance_m)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (mid, e["t"], e["frame"], e["phase"], e["chrono"], e["type"],
                         prof.get(e["tueur"]) if e["tueur"] else None, prof.get(e["victime"]) if e["victime"] else None,
                         e["texte"] + (" (coéquipier)" if allie(e) else ""), allie(e), *(map(float, kp) if kp is not None else (None,) * 3),
                         *(map(float, vp) if vp is not None else (None,) * 3), dist))
        with cur.copy("COPY position (manche_id, profil, image, t_s, x, y, z) FROM STDIN") as cp:
            for p in R["entries"]:
                pid = prof[p["pseudo"]]
                for r, t in zip(p["track"], p["t_track"]):
                    cp.write_row((mid, pid, int(r[0]), round(float(t), 3), float(r[1]), float(r[2]), float(r[3])))
        with cur.copy("COPY orientation (manche_id, profil, image, t_s, lacet, tangage) FROM STDIN") as cp:
            for p in R["entries"]:
                if p["facing"] is None:
                    continue
                pid = prof[p["pseudo"]]
                for r, t in zip(p["facing"], p["t_facing"]):
                    cp.write_row((mid, pid, int(r[0]), round(float(t), 3), float(r[1]),
                                  None if math.isnan(r[2]) else float(r[2])))
    db.commit()
    doubt = [p["pseudo"] for p in R["entries"] if "INCERTAIN" in p["methode"] or "hasard" in p["methode"]]
    msg = f"partie {pnum}, manche {R['numero']} (id {mid}) : {R['title']}, {len(R['entries'])} joueurs, {len(R['events'])} événements"
    if doubt:
        msg += f"  ⚠ identification incertaine : {', '.join(doubt)} (voir : python r6_db.py journal {mid})"
    return msg


def cmd_import(db, a):
    files = list(find_rec(a.chemins))
    if not files:
        sys.exit("Aucun fichier .rec trouvé.")
    if a.noms and len(files) > 1:
        sys.exit("--noms ne s'utilise qu'avec un seul fichier.")
    ok = 0
    for f in files:
        try:
            msg = import_one(db, f, a.noms, a.remplacer)
            ok += msg.startswith("partie ")
        except (ValueError, SystemExit) as err:
            db.rollback()
            msg = f"ÉCHEC : {err}"
        except Exception as err:                                   # un fichier abîmé ne bloque pas les autres
            db.rollback()
            msg = f"ÉCHEC ({type(err).__name__}) : {err}"
        print(f"{os.path.basename(f)} -> {msg}")
    print(f"\n{ok} manche(s) importée(s) sur {len(files)} fichier(s).")


# ----------------------------------------------------------------------------- consultation
def print_table(cur, title=None):
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    if title:
        print(f"\n{title}")
    if not rows:
        print("  (rien)")
        return
    txt = [[("" if v is None else str(v)) for v in r] for r in rows]
    w = [max(len(c), *(len(r[i]) for r in txt)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("-" * w[i] for i in range(len(cols))))
    for r in txt:
        print("  " + "  ".join(v.ljust(w[i]) for i, v in enumerate(r)))


def resolve_manche(db, a):
    """Identifiant de manche à partir de l'id global, ou de --partie N --manche K."""
    if a.partie is not None and a.manche_num is not None:
        with db.cursor() as cur:
            cur.execute("SELECT m.id FROM manche m JOIN partie p ON p.id = m.partie_id WHERE p.num = %s AND m.numero = %s",
                        (a.partie, a.manche_num))
            row = cur.fetchone()
        if not row:
            sys.exit(f"Pas de manche {a.manche_num} dans la partie {a.partie} (voir : python r6_db.py liste --partie {a.partie}).")
        return row[0]
    if a.id is None:
        sys.exit("Indique une manche : son id (colonne id de « liste ») ou --partie N --manche K.")
    return a.id


def cmd_parties(db, a):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM v_parties")
        print_table(cur)


def cmd_liste(db, a):
    with db.cursor() as cur:
        if a.partie is None:
            cur.execute("SELECT * FROM v_manches")
        else:
            cur.execute("SELECT * FROM v_manches WHERE partie = %s", (a.partie,))
        print_table(cur)


def cmd_journal(db, a):
    mid = resolve_manche(db, a)
    with db.cursor() as cur:
        cur.execute("SELECT journal FROM manche WHERE id = %s", (mid,))
        row = cur.fetchone()
    if not row:
        sys.exit(f"Pas de manche {mid}.")
    print(row[0])


def viewer_from_db(db, mid):
    """Données du lecteur HTML d'une manche, entièrement lues dans la base."""
    with db.cursor() as cur:
        cur.execute("SELECT titre, phases, horloge, etages, duree_s FROM manche WHERE id = %s", (mid,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"Pas de manche {mid}.")
        titre, phases, horloge, etages, fin = row
        cur.execute("""SELECT profil, pseudo, operateur, role, couleur, entite, mort_s FROM participation
                       WHERE manche_id = %s ORDER BY role, lower(pseudo)""", (mid,))
        joueurs = []
        for profil, pseudo, op, role, col, ent, mort in cur.fetchall():
            with db.cursor() as c2:
                c2.execute("SELECT t_s, x, y, z FROM position WHERE manche_id = %s AND profil = %s ORDER BY t_s",
                           (mid, profil))
                pos = np.array(c2.fetchall(), dtype=float).reshape(-1, 4)
                c2.execute("SELECT t_s, lacet, tangage FROM orientation WHERE manche_id = %s AND profil = %s ORDER BY t_s",
                           (mid, profil))
                ori = c2.fetchall()
            if not len(pos):
                continue
            joueurs.append({"pseudo": pseudo, "operateur": op, "role": role, "couleur": col, "entite": ent,
                            "mort": mort, "points": T.resample(pos[:, 0], pos[:, 1:4], mort),
                            "vue": T.resample_facing([o[0] for o in ori], [o[1] for o in ori],
                                                     [float("nan") if o[2] is None else o[2] for o in ori],
                                                     float(pos[0, 0]), float(pos[-1, 0]))})
        cur.execute("""SELECT e.t_s, e.type, e.texte, e.phase, e.chrono, coalesce(pt.pseudo, ''), coalesce(pv.pseudo, '')
                       FROM evenement e
                       LEFT JOIN participation pt ON pt.manche_id = e.manche_id AND pt.profil = e.tueur
                       LEFT JOIN participation pv ON pv.manche_id = e.manche_id AND pv.profil = e.victime
                       WHERE e.manche_id = %s ORDER BY e.t_s""", (mid,))
        events = [dict(zip(("t", "type", "texte", "phase", "chrono", "tueur", "victime"), r)) for r in cur.fetchall()]
    return T.viewer_data(titre, joueurs, events, phases, etages, fin, horloge)


INDEX_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Partie {num}</title>
<style>
 :root {{ --bg:#f6f7f9; --panel:#fff; --ink:#1d2129; --muted:#6b7280; --line:#e3e6eb; --ok:#2b8a3e; --ko:#c92a2a; }}
 @media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#111418; --panel:#1a1e24; --ink:#e8eaed; --muted:#9aa1ab; --line:#2a3038; --ok:#69db7c; --ko:#ff8787; }} }}
 :root[data-theme="dark"] {{ --bg:#111418; --panel:#1a1e24; --ink:#e8eaed; --muted:#9aa1ab; --line:#2a3038; --ok:#69db7c; --ko:#ff8787; }}
 body {{ margin:0; padding:20px 16px; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
 main {{ max-width:900px; margin:0 auto; }}
 h1 {{ font-size:20px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 16px; }}
 .wrap {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
 table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
 th, td {{ text-align:left; padding:8px 12px; border-top:1px solid var(--line); white-space:nowrap; }}
 th {{ border-top:0; color:var(--muted); font-weight:600; font-size:13px; }}
 a {{ color:inherit; font-weight:600; }} .gagnée {{ color:var(--ok); }} .perdue {{ color:var(--ko); }}
</style></head><body><main>
<h1>Partie {num} · {carte}</h1><p>{date} · score final {score} · {resultat}</p>
<div class="wrap"><table><thead><tr><th>Manche</th><th>Rôle</th><th>Résultat</th><th>Condition</th><th>Score</th><th>Durée</th><th>Kills</th></tr></thead>
<tbody>{lignes}</tbody></table></div>
</main></body></html>
"""


def cmd_voir(db, a):
    import html
    if a.partie is not None and a.manche_num is None:            # toute la partie : un dossier avec un index
        with db.cursor() as cur:
            cur.execute("SELECT partie, date, carte, score, resultat FROM v_parties WHERE partie = %s", (a.partie,))
            info = cur.fetchone()
            if not info:
                sys.exit(f"Pas de partie {a.partie} (voir : python r6_db.py parties).")
            cur.execute("""SELECT manche, id, role_enregistreur, resultat, condition, score, duree_s, kills
                           FROM v_manches WHERE partie = %s ORDER BY manche""", (a.partie,))
            manches = cur.fetchall()
        outdir = a.out or f"partie_{a.partie}"
        os.makedirs(outdir, exist_ok=True)
        lignes = []
        for num, mid, role, res, cond, score, duree, kills in manches:
            name = f"manche_{num:02d}.html"
            T.write_viewer(viewer_from_db(db, mid), os.path.join(outdir, name))
            e = lambda v: html.escape("" if v is None else str(v))
            lignes.append(f'<tr><td><a href="{name}">Manche {num}</a></td><td>{e(role)}</td>'
                          f'<td class="{e(res)}">{e(res)}</td><td>{e(cond)}</td><td>{e(score)}</td>'
                          f'<td>{int(duree) // 60}:{int(duree) % 60:02d}</td><td>{e(kills)}</td></tr>')
            print(f"  {name}")
        num, date, carte, score, resultat = info
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(INDEX_HTML.format(num=num, carte=html.escape(carte or "?"),
                                      date=date.strftime("%d/%m/%Y %H:%M") if date else "?",
                                      score=html.escape(score or "?"), resultat=html.escape(resultat or "?"),
                                      lignes="\n".join(lignes)))
        print(f"Partie {a.partie} : {len(manches)} manche(s) -> {outdir}/index.html")
        return
    mid = resolve_manche(db, a)
    out = a.out or (f"partie_{a.partie}_manche_{a.manche_num}.html" if a.partie is not None else f"manche_{mid}.html")
    T.write_viewer(viewer_from_db(db, mid), out)
    print(f"Lecteur écrit : {out}")


def cmd_stats(db, a):
    p = a.partie
    scope = f" (partie {p})" if p is not None else " (toutes les parties)"
    with db.cursor() as cur:
        if not a.joueur:
            cur.execute("SELECT pseudo, manches, parties, kills, morts, kd, kills_par_manche, survie_pct, victoires_pct, "
                        "premiers_kills, premieres_morts, distance_moy_m FROM stats_joueur(%s) "
                        "ORDER BY manches DESC, kd DESC NULLS LAST", (p,))
            print_table(cur, "Bilan par joueur" + scope)
            cur.execute("SELECT tueur, victime, kills, distance_moy_m FROM duels(%s) ORDER BY kills DESC, tueur LIMIT 15", (p,))
            print_table(cur, "Duels les plus fréquents")
            cur.execute("SELECT tueur, victime, partie, manche, round(t_s::numeric, 1) AS t_s FROM kills_allies(%s) "
                        "ORDER BY partie, manche", (p,))
            print_table(cur, "Kills sur des coéquipiers (non comptés)")
            return
        like = a.joueur
        cur.execute("SELECT * FROM stats_joueur(%s) WHERE pseudo ILIKE %s", (p, like))
        print_table(cur, f"Bilan de {like}" + scope)
        cur.execute("SELECT role, operateur, manches, kills, morts, victoires_pct FROM stats_operateur(%s) "
                    "WHERE pseudo ILIKE %s ORDER BY manches DESC, kills DESC", (p, like))
        print_table(cur, "Par opérateur")
        cur.execute("SELECT carte, role, manches, victoires_pct, kills_par_manche FROM stats_carte(%s) "
                    "WHERE pseudo ILIKE %s ORDER BY carte, role", (p, like))
        print_table(cur, "Par carte et par rôle")
        cur.execute("SELECT victime, kills, distance_moy_m FROM duels(%s) WHERE tueur ILIKE %s ORDER BY kills DESC", (p, like))
        print_table(cur, "Ses victimes")
        cur.execute("SELECT tueur, kills AS fois, distance_moy_m FROM duels(%s) WHERE victime ILIKE %s ORDER BY kills DESC",
                    (p, like))
        print_table(cur, "Tué par")


def main():
    ap = argparse.ArgumentParser(description="Base PostgreSQL des replays R6 Siege")
    ap.add_argument("--db", default=os.environ.get("R6_DB", DEFAULT_DB), help="URL PostgreSQL (ou variable R6_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="crée les tables et les vues")
    p = sub.add_parser("import", help="importe des .rec (fichiers ou dossiers)")
    p.add_argument("chemins", nargs="+")
    p.add_argument("--remplacer", action="store_true", help="écrase une manche déjà importée")
    p.add_argument("--noms", default="", help='corrige les attributions (un seul fichier), ex. "0xf004c777=nitramossur1"')
    sub.add_parser("parties", help="parties enregistrées (avec leur numéro)")
    p = sub.add_parser("liste", help="manches enregistrées")
    p.add_argument("--partie", type=int, help="seulement cette partie")
    for name, hlp in (("voir", "écrit le lecteur HTML d'une manche, ou de toutes les manches d'une partie"),
                      ("journal", "affiche le résumé d'analyse d'une manche")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("id", type=int, nargs="?", help="id de la manche (colonne id de « liste »)")
        p.add_argument("--partie", type=int, help="numéro de partie (voir « parties »)")
        p.add_argument("--manche", type=int, dest="manche_num", help="numéro de la manche dans la partie")
        if name == "voir":
            p.add_argument("-o", "--out", help="fichier (une manche) ou dossier (toute une partie)")
    p = sub.add_parser("stats", help="statistiques")
    p.add_argument("--joueur", help="pseudo (accepte % comme joker)")
    p.add_argument("--partie", type=int, help="seulement cette partie")
    a = ap.parse_args()
    db = connect(a.db)
    try:
        {"init": cmd_init, "import": cmd_import, "parties": cmd_parties, "liste": cmd_liste, "voir": cmd_voir,
         "journal": cmd_journal, "stats": cmd_stats}[a.cmd](db, a)
    finally:
        db.close()


if __name__ == "__main__":
    main()
