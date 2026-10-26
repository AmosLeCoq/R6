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

-- Numéro court de partie (1, 2, 3... dans l'ordre d'import), ajouté aux bases existantes par init
ALTER TABLE partie ADD COLUMN IF NOT EXISTS num int UNIQUE;
UPDATE partie SET num = s.n
FROM (SELECT id, (SELECT coalesce(max(num), 0) FROM partie) + row_number() OVER (ORDER BY date_debut, importe_le) AS n
      FROM partie WHERE num IS NULL) s
WHERE partie.id = s.id;

-- ---------------------------------------------------------------------------- vues de statistiques
-- Recréées à chaque init (les définitions peuvent changer d'une version à l'autre).
DROP VIEW IF EXISTS v_manches, v_parties, v_kills_allies, v_duels, v_stats_carte, v_stats_operateur,
                    v_stats_joueur, v_participation CASCADE;
DROP FUNCTION IF EXISTS stats_joueur(int), stats_operateur(int), stats_carte(int), duels(int), kills_allies(int) CASCADE;

-- Une ligne par joueur et par manche, avec le résultat de la manche de son point de vue
CREATE VIEW v_participation AS
SELECT pa.*, m.partie_id, m.numero, p.carte, p.date_debut,
       (m.vainqueur = pa.equipe) AS victoire,
       (pa.mort_s IS NULL) AS survie,
       EXISTS (SELECT 1 FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill' AND NOT e.allie AND e.tueur = pa.profil
               AND e.t_s = (SELECT min(t_s) FROM evenement e2 WHERE e2.manche_id = m.id AND e2.type = 'kill' AND NOT e2.allie)) AS premier_kill,
       EXISTS (SELECT 1 FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill' AND NOT e.allie AND e.victime = pa.profil
               AND e.t_s = (SELECT min(t_s) FROM evenement e2 WHERE e2.manche_id = m.id AND e2.type = 'kill' AND NOT e2.allie)) AS premiere_mort,
       p.num AS partie
FROM participation pa
JOIN manche m ON m.id = pa.manche_id
JOIN partie p ON p.id = m.partie_id;

-- Les fonctions prennent un numéro de partie (NULL = toutes les parties) : SELECT * FROM stats_joueur(2);

-- Bilan par joueur
CREATE FUNCTION stats_joueur(p_partie int DEFAULT NULL) RETURNS TABLE (
    profil uuid, pseudo text, manches bigint, parties bigint, kills bigint, morts bigint, kd numeric,
    kills_par_manche numeric, survie_pct numeric, victoires_pct numeric, premiers_kills bigint,
    premieres_morts bigint, distance_moy_m numeric) LANGUAGE sql STABLE AS $$
SELECT j.profil, j.pseudo,
       count(*),
       count(DISTINCT v.partie_id),
       sum(v.kills),
       count(*) FILTER (WHERE NOT v.survie),
       round(sum(v.kills)::numeric / GREATEST(count(*) FILTER (WHERE NOT v.survie), 1), 2),
       round(sum(v.kills)::numeric / count(*), 2),
       round(100.0 * count(*) FILTER (WHERE v.survie) / count(*), 1),
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1),
       count(*) FILTER (WHERE v.premier_kill),
       count(*) FILTER (WHERE v.premiere_mort),
       round(avg(v.distance_m)::numeric, 0)
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
WHERE p_partie IS NULL OR v.partie = p_partie
GROUP BY j.profil, j.pseudo
$$;

-- Bilan par joueur, rôle et opérateur
CREATE FUNCTION stats_operateur(p_partie int DEFAULT NULL) RETURNS TABLE (
    pseudo text, role text, operateur text, manches bigint, kills bigint, morts bigint, victoires_pct numeric)
    LANGUAGE sql STABLE AS $$
SELECT j.pseudo, v.role, v.operateur, count(*), sum(v.kills), count(*) FILTER (WHERE NOT v.survie),
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1)
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
WHERE p_partie IS NULL OR v.partie = p_partie
GROUP BY j.pseudo, v.role, v.operateur
$$;

-- Résultats par carte et par rôle, du point de vue de chaque joueur
CREATE FUNCTION stats_carte(p_partie int DEFAULT NULL) RETURNS TABLE (
    pseudo text, carte text, role text, manches bigint, victoires_pct numeric, kills_par_manche numeric)
    LANGUAGE sql STABLE AS $$
SELECT j.pseudo, v.carte, v.role, count(*),
       round(100.0 * count(*) FILTER (WHERE v.victoire) / count(*), 1),
       round(sum(v.kills)::numeric / count(*), 2)
FROM v_participation v
JOIN joueur j ON j.profil = v.profil
WHERE p_partie IS NULL OR v.partie = p_partie
GROUP BY j.pseudo, v.carte, v.role
$$;

-- Qui tue qui (hors coéquipiers)
CREATE FUNCTION duels(p_partie int DEFAULT NULL) RETURNS TABLE (
    tueur text, victime text, kills bigint, distance_moy_m numeric) LANGUAGE sql STABLE AS $$
SELECT jt.pseudo, jv.pseudo, count(*), round(avg(e.distance_m)::numeric, 1)
FROM evenement e
JOIN manche m ON m.id = e.manche_id
JOIN partie p ON p.id = m.partie_id
JOIN joueur jt ON jt.profil = e.tueur
JOIN joueur jv ON jv.profil = e.victime
WHERE e.type = 'kill' AND NOT e.allie AND (p_partie IS NULL OR p.num = p_partie)
GROUP BY jt.pseudo, jv.pseudo
$$;

-- Kills sur des coéquipiers
CREATE FUNCTION kills_allies(p_partie int DEFAULT NULL) RETURNS TABLE (
    tueur text, victime text, partie int, manche int, manche_id int, t_s real) LANGUAGE sql STABLE AS $$
SELECT jt.pseudo, jv.pseudo, p.num, m.numero, m.id, e.t_s
FROM evenement e
JOIN manche m ON m.id = e.manche_id
JOIN partie p ON p.id = m.partie_id
JOIN joueur jt ON jt.profil = e.tueur
JOIN joueur jv ON jv.profil = e.victime
WHERE e.type = 'kill' AND e.allie AND (p_partie IS NULL OR p.num = p_partie)
$$;

CREATE VIEW v_stats_joueur AS SELECT * FROM stats_joueur(NULL);
CREATE VIEW v_stats_operateur AS SELECT * FROM stats_operateur(NULL);
CREATE VIEW v_stats_carte AS SELECT * FROM stats_carte(NULL);
CREATE VIEW v_duels AS SELECT * FROM duels(NULL);
CREATE VIEW v_kills_allies AS SELECT * FROM kills_allies(NULL);

-- Liste des parties, avec le résultat de l'équipe du joueur qui a enregistré
CREATE VIEW v_parties AS
WITH der AS (
    SELECT DISTINCT ON (m.partie_id) m.partie_id, m.score0_apres, m.score1_apres, m.enregistre_par, m.id
    FROM manche m ORDER BY m.partie_id, m.numero DESC
), equipe_rec AS (
    SELECT der.partie_id, pa.equipe FROM der JOIN participation pa ON pa.manche_id = der.id AND pa.profil = der.enregistre_par
)
SELECT p.num AS partie, (SELECT min(date_manche) FROM manche m WHERE m.partie_id = p.id) AS date,
       p.carte, (SELECT count(*) FROM manche m WHERE m.partie_id = p.id) AS manches_en_base,
       der.score0_apres || '-' || der.score1_apres AS score,
       j.pseudo AS enregistre_par,
       CASE WHEN er.equipe IS NULL THEN NULL
            WHEN (CASE er.equipe WHEN '0' THEN der.score0_apres - der.score1_apres
                                 ELSE der.score1_apres - der.score0_apres END) > 0 THEN 'victoire'
            WHEN der.score0_apres = der.score1_apres THEN 'égalité' ELSE 'défaite' END AS resultat
FROM partie p
LEFT JOIN der ON der.partie_id = p.id
LEFT JOIN equipe_rec er ON er.partie_id = p.id
LEFT JOIN joueur j ON j.profil = der.enregistre_par
ORDER BY p.num;

-- Liste des manches
CREATE VIEW v_manches AS
SELECT p.num AS partie, m.numero AS manche, m.id, m.date_manche, p.carte, pa.role AS role_enregistreur,
       CASE WHEN m.vainqueur IS NULL OR pa.equipe IS NULL THEN NULL
            WHEN m.vainqueur = pa.equipe THEN 'gagnée' ELSE 'perdue' END AS resultat,
       m.condition, m.score0_apres || '-' || m.score1_apres AS score, round(m.duree_s::numeric, 0) AS duree_s,
       j.pseudo AS enregistre_par,
       (SELECT count(*) FROM evenement e WHERE e.manche_id = m.id AND e.type = 'kill') AS kills
FROM manche m
JOIN partie p ON p.id = m.partie_id
LEFT JOIN joueur j ON j.profil = m.enregistre_par
LEFT JOIN participation pa ON pa.manche_id = m.id AND pa.profil = m.enregistre_par
ORDER BY p.num, m.numero;
