-- Schéma PostgreSQL des replays R6 Siege (créé par : python r6_db.py init)
-- Une partie (match) contient des manches ; dans chaque manche, chaque joueur a une participation,
-- des positions et des orientations ; les événements (kills, messages) sont datés.

CREATE TABLE IF NOT EXISTS partie (
    id              uuid PRIMARY KEY,                 -- identifiant du match (en-tête "id")
    date_debut      timestamptz,
    carte           text,
    worldid         bigint,
    version         text,                             -- saison du jeu, ex. Y11S3_Alpha04
    mode            text,                             -- gamemodeid
    type_partie     text,                             -- matchtype
    equipe0_nom     text,
    equipe1_nom     text,
    importe_le      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS joueur (
    profil          uuid PRIMARY KEY,                 -- profileid Ubisoft (stable même si le pseudo change)
    pseudo          text NOT NULL,                    -- dernier pseudo vu
    vu_le           timestamptz
);

CREATE TABLE IF NOT EXISTS manche (
    id              serial PRIMARY KEY,
    partie_id       uuid NOT NULL REFERENCES partie(id) ON DELETE CASCADE,
    numero          int NOT NULL,                     -- 1 = première manche
    date_manche     timestamptz,
    fichier         text,
    sha256          text NOT NULL UNIQUE,             -- empreinte du .rec : un fichier n'est importé qu'une fois
    enregistre_par  uuid REFERENCES joueur(profil),
    equipe_attaque  text,                             -- '0' ou '1'
    vainqueur       text,                             -- '0' ou '1'
    condition       text,                             -- élimination, désamorceur explosé / désactivé, temps écoulé...
    score0_avant    int, score1_avant int, score0_apres int, score1_apres int,
    duree_s         real,                             -- du début de la préparation à la fin de la manche
    titre           text,
    phases          jsonb,                            -- [{nom, debut, duree}]
    horloge         jsonb,                            -- {t: [...], restant: [...], phase: [...]} pour le chrono du lecteur
    etages          jsonb,                            -- niveaux détectés [[z_min, z_max, z_typique]]
    journal         text,                             -- resume.txt de l'analyse
    UNIQUE (partie_id, numero)
);

CREATE TABLE IF NOT EXISTS participation (
    manche_id       int NOT NULL REFERENCES manche(id) ON DELETE CASCADE,
    profil          uuid NOT NULL REFERENCES joueur(profil),
    pseudo          text NOT NULL,                    -- pseudo au moment de la manche
    operateur       text,
    equipe          text,                             -- '0' ou '1'
    role            text,                             -- 'attaque' ou 'défense'
    entite          text,                             -- identifiant d'entité dans le replay
    identification  text,                             -- comment l'entité a été reliée au joueur
    couleur         text,
    mort_s          real,                             -- NULL = a survécu
    kills           int NOT NULL DEFAULT 0,           -- hors coéquipiers
    distance_m      real,                             -- distance parcourue (plan horizontal)
    PRIMARY KEY (manche_id, profil)
);

CREATE TABLE IF NOT EXISTS evenement (
    id              bigserial PRIMARY KEY,
    manche_id       int NOT NULL REFERENCES manche(id) ON DELETE CASCADE,
    t_s             real NOT NULL,
    image           int,
    phase           text,
    chrono          text,                             -- temps restant affiché en jeu
    type            text NOT NULL,                    -- kill, message, fin
    tueur           uuid REFERENCES joueur(profil),
    victime         uuid REFERENCES joueur(profil),
    texte           text,
    allie           boolean NOT NULL DEFAULT false,   -- kill sur un coéquipier (non compté dans les stats)
    tueur_x real, tueur_y real, tueur_z real,         -- positions au moment du kill (si connues)
    victime_x real, victime_y real, victime_z real,
    distance_m      real
);

CREATE TABLE IF NOT EXISTS position (
    manche_id       int NOT NULL REFERENCES manche(id) ON DELETE CASCADE,
    profil          uuid NOT NULL,
    image           int NOT NULL,
    t_s             real NOT NULL,
    x real NOT NULL, y real NOT NULL, z real NOT NULL
);

CREATE TABLE IF NOT EXISTS orientation (
    manche_id       int NOT NULL REFERENCES manche(id) ON DELETE CASCADE,
    profil          uuid NOT NULL,
    image           int NOT NULL,
    t_s             real NOT NULL,
    lacet           real NOT NULL,                    -- degrés, direction horizontale (repère de la carte)
    tangage         real                              -- degrés, + vers le haut ; NULL si la caméra est inconnue
);

CREATE INDEX IF NOT EXISTS position_manche_profil_t ON position (manche_id, profil, t_s);
CREATE INDEX IF NOT EXISTS orientation_manche_profil_t ON orientation (manche_id, profil, t_s);
CREATE INDEX IF NOT EXISTS evenement_manche ON evenement (manche_id, t_s);
CREATE INDEX IF NOT EXISTS participation_profil ON participation (profil);

-- ---------------------------------------------------------------------------- vues de statistiques

-- Une ligne par joueur et par manche, avec le résultat de la manche de son point de vue
CREATE OR REPLACE VIEW v_participation AS
SELECT pa.*, m.partie_id, m.numero, p.carte, p.date_debut,
       (m.vainqueur = pa.equipe) AS victoire,
       (pa.mort_s IS NULL) AS survie,
       EXISTS (SELECT 1 FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill' AND NOT e.allie AND e.tueur = pa.profil
               AND e.t_s = (SELECT min(t_s) FROM evenement e2 WHERE e2.manche_id = m.id AND e2.type = 'kill' AND NOT e2.allie)) AS premier_kill,
       EXISTS (SELECT 1 FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill' AND NOT e.allie AND e.victime = pa.profil
               AND e.t_s = (SELECT min(t_s) FROM evenement e2 WHERE e2.manche_id = m.id AND e2.type = 'kill' AND NOT e2.allie)) AS premiere_mort
FROM participation pa
JOIN manche m ON m.id = pa.manche_id
JOIN partie p ON p.id = m.partie_id;

-- Bilan par joueur
CREATE OR REPLACE VIEW v_stats_joueur AS
SELECT j.profil, j.pseudo,
       count(*)                                                     AS manches,
       count(DISTINCT v.partie_id)                                  AS parties,
       sum(v.kills)                                                 AS kills,
       count(*) FILTER (WHERE NOT v.survie)                         AS morts,
       round(sum(v.kills)::numeric / GREATEST(count(*) FILTER (WHERE NOT v.survie), 1), 2) AS kd,
       round(sum(v.kills)::numeric / count(*), 2)                   AS kills_par_manche,
       round(100.0 * count(*) FILTER (WHERE v.survie) / count(*), 1)    AS survie_pct,
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1)  AS victoires_pct,
       count(*) FILTER (WHERE v.premier_kill)                       AS premiers_kills,
       count(*) FILTER (WHERE v.premiere_mort)                      AS premieres_morts,
       round(avg(v.distance_m)::numeric, 0)                         AS distance_moy_m
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
GROUP BY j.profil, j.pseudo;

-- Bilan par joueur, rôle et opérateur
CREATE OR REPLACE VIEW v_stats_operateur AS
SELECT j.pseudo, v.role, v.operateur,
       count(*)                                                     AS manches,
       sum(v.kills)                                                 AS kills,
       count(*) FILTER (WHERE NOT v.survie)                         AS morts,
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1)  AS victoires_pct
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
GROUP BY j.pseudo, v.role, v.operateur;

-- Qui tue qui
CREATE OR REPLACE VIEW v_duels AS
SELECT jt.pseudo AS tueur, jv.pseudo AS victime, count(*) AS kills,
       round(avg(e.distance_m)::numeric, 1) AS distance_moy_m
FROM evenement e
JOIN joueur jt ON jt.profil = e.tueur
JOIN joueur jv ON jv.profil = e.victime
WHERE e.type = 'kill' AND NOT e.allie
GROUP BY jt.pseudo, jv.pseudo;

-- Kills sur des coéquipiers
CREATE OR REPLACE VIEW v_kills_allies AS
SELECT jt.pseudo AS tueur, jv.pseudo AS victime, m.id AS manche_id, e.t_s
FROM evenement e
JOIN manche m ON m.id = e.manche_id
JOIN joueur jt ON jt.profil = e.tueur
JOIN joueur jv ON jv.profil = e.victime
WHERE e.type = 'kill' AND e.allie;

-- Résultats par carte et par rôle, du point de vue de chaque joueur
CREATE OR REPLACE VIEW v_stats_carte AS
SELECT j.pseudo, v.carte, v.role,
       count(*)                                                     AS manches,
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1)  AS victoires_pct,
       round(sum(v.kills)::numeric / count(*), 2)                   AS kills_par_manche
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
GROUP BY j.pseudo, v.carte, v.role;

-- Liste des manches
CREATE OR REPLACE VIEW v_manches AS
SELECT m.id, m.date_manche, p.carte, m.numero, m.vainqueur, m.condition,
       m.score0_apres || '-' || m.score1_apres AS score, round(m.duree_s::numeric, 0) AS duree_s,
       j.pseudo AS enregistre_par,
       (SELECT count(*) FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill') AS kills
FROM manche m
JOIN partie p ON p.id = m.partie_id
LEFT JOIN joueur j ON j.profil = m.enregistre_par
ORDER BY m.date_manche, m.numero;
