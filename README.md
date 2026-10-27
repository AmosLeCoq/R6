# R6 — reconstitution de manche à partir d'un replay Rainbow Six Siege

`r6_replay_tool.py` lit un replay `.rec` de Rainbow Six Siege et reconstitue la manche entière :
qui est où, à quel moment, et ce qui se passe (kills, messages, fin de manche).

```
pip install -r requirements.txt
python r6_replay_tool.py Match-XXXX-R09.rec
```

Les replays se trouvent dans le dossier du jeu, sous `MatchReplay/<match>/Match-...-RXX.rec` (un fichier par manche).

## Base de données (plusieurs parties, statistiques)

`r6_db.py` enregistre les manches dans PostgreSQL, avec la même analyse que `r6_replay_tool.py`.

```
sudo apt install postgresql                # ou : docker compose up -d
sudo -u postgres psql -c "CREATE USER r6 WITH PASSWORD 'r6';" -c "CREATE DATABASE r6 OWNER r6;"
python r6_db.py init                       # tables et vues (à relancer après une mise à jour du code)
python r6_db.py import ~/chemin/MatchReplay/   # tous les .rec du dossier, sous-dossiers compris
python r6_db.py parties                    # parties enregistrées, avec leur numéro (1, 2, 3...)
python r6_db.py liste --partie 2           # manches de la partie 2
python r6_db.py voir --partie 2            # toutes les manches de la partie 2 : dossier partie_2/ + index.html
python r6_db.py voir --partie 2 --manche 5 # une seule manche
python r6_db.py stats                      # bilan par joueur, duels, kills sur coéquipiers (toutes les parties)
python r6_db.py stats --partie 2           # la même chose pour une seule partie
python r6_db.py stats --joueur nitramossur1    # opérateurs, cartes, victimes, tué par
python r6_db.py journal --partie 2 --manche 5  # résumé d'analyse (identification des joueurs)
```

### Trajets d'un joueur (plusieurs manches et parties superposées)

```
python r6_db.py trajets --joueur nitramossur1 --carte Littoral --role attaque
python r6_db.py trajets --joueur nitramossur1 --carte Littoral --role attaque --dernieres 20
python r6_db.py trajets --joueur nitramossur1 --carte Littoral --site narguilé
```

La page superpose les trajets de toutes les manches qui correspondent, alignés sur le début de l'action
(le temps négatif est la préparation) : fenêtre de temps réglable (« que fait-il dans les 30 premières
secondes ? »), carte de chaleur du temps passé, kills (étoile, avec un trait vers la victime) et morts (croix),
temps passé par niveau, et une liste des manches à cocher. Au-delà de 12 manches, les trajets passent en
mode densité (une couleur, semi-transparente) pour faire ressortir les chemins habituels.

- `--dernieres N` : les N dernières parties de ce joueur sur cette carte (dans le rôle demandé).
- `--site` : site de la bombe ; un morceau du nom suffit (« narguilé », « cuisine »). Les noms sont ceux du
  jeu, dans sa langue. `python r6_db.py liste` affiche le site de chaque manche.
- Pour faire ses propres requêtes : la vue `v_positions` donne chaque position avec la partie, la manche,
  la carte, le site, le joueur, le rôle, l'opérateur et `t_action` (secondes depuis le début de l'action).

### Plan de la carte

Le lecteur peut afficher une image de la carte sous les joueurs (une image par niveau). Le script ne fournit
pas d'images : utilise une capture ou un plan de la carte.

1. Ouvre une manche (`voir`), choisis le niveau avec les boutons « Niveau », puis ouvre le panneau
   « Plan de la carte » et charge ton image.
2. Cale-la sur les traces grises des joueurs : glisser pour la déplacer, Maj + molette pour l'agrandir,
   Alt + molette ou le champ « Rotation » pour la tourner. Le calage est retenu par le navigateur.
3. Copie la commande affichée en bas du panneau, remplace `CHEMIN/VERS/...` par le vrai chemin de
   l'image et lance-la. Le plan est enregistré dans la base et ajouté à tous les lecteurs de cette carte
   (régénère-les avec `voir`).

```
python r6_db.py carte                                   # cartes en base (worldid, nom, nombre de plans)
python r6_db.py carte 436375283234 "Nom de la carte"    # nomme une carte inconnue (titres et stats)
python r6_db.py plan --liste                            # plans enregistrés
python r6_db.py plan --worldid 436375283234 --z 2.9 --calage "..."     # recaler un plan existant
python r6_db.py plan --worldid 436375283234 --z 2.9 --supprimer
```

- Connexion : variable `R6_DB` ou option `--db` (défaut `postgresql://r6:r6@localhost:5432/r6`).
- Chaque match est une `partie` distincte (identifiant du match écrit dans les `.rec`), avec un numéro court
  attribué à l'import. Chaque manche a aussi un `id` unique dans toute la base : `voir 37` marche aussi.
- Le site de la bombe est lu dans le replay. Pour les manches importées avant cette fonction, relancer
  `import` sur les mêmes fichiers ajoute seulement le site (rapide, corrections `--noms` conservées).
- Le lecteur HTML est reconstruit entièrement depuis la base : les `.rec` ne sont plus nécessaires après l'import.
- Un fichier n'est importé qu'une fois (empreinte SHA-256). Une même manche enregistrée par deux joueurs
  n'est gardée qu'une fois. `--remplacer` réimporte.
- Si une attribution est fausse : `python r6_db.py import fichier.rec --noms "0x...=pseudo" --remplacer`.
- Les joueurs sont identifiés par leur `profileid` Ubisoft : les stats suivent un joueur même s'il change de pseudo.
- Les kills sur un coéquipier sont enregistrés mais ne comptent ni dans les kills, ni dans les duels.

Tables : `partie`, `manche`, `joueur`, `carte`, `plan_carte`, `participation` (un joueur dans une manche : opérateur, rôle, mort,
kills, distance parcourue), `evenement` (avec la position du tueur et de la victime au moment du kill),
`position`, `orientation`. Vues : `v_parties`, `v_manches`, `v_participation`, `v_stats_joueur`,
`v_stats_operateur`, `v_stats_carte`, `v_stats_site`, `v_duels`, `v_kills_allies`, `v_positions`. Les mêmes statistiques existent en
fonctions avec un numéro de partie (`NULL` = toutes) :

```sql
SELECT pseudo, kd, victoires_pct FROM v_stats_joueur ORDER BY kd DESC;
SELECT * FROM stats_joueur(2);      -- partie 2 seulement
SELECT * FROM duels(2);
```

## Sorties de r6_replay_tool.py

Dans `sortie_<nom du replay>/` (ou le dossier passé avec `-o`) :

| Fichier | Contenu |
| --- | --- |
| `manche.html` | lecteur interactif : vue de dessus, champ de vision de chaque joueur, curseur de temps, lecture (espace), vitesse, traînées, filtre par niveau, liste des événements cliquable |
| `evenements.csv` | kills et messages datés : temps du replay, phase, chrono affiché en jeu, numéro d'image |
| `positions.csv` | une ligne par position : pseudo, opérateur, rôle, entité, image, temps, x, y, z |
| `orientations.csv` | direction du regard : lacet (horizontal, en degrés) et, quand la caméra du joueur est trouvée, tangage (+ vers le haut, − vers le bas) |
| `trajectoires.png` | vue d'ensemble, tous les joueurs nommés |
| `resume.txt` | en-tête (carte, version, score), phases de l'horloge, correspondance entités → joueurs, couverture des données |

Le temps `t_replay_s` part du début de la phase de préparation. Le chrono est le temps restant
affiché en jeu dans la phase en cours.

## Corriger un nom

Les positions sont rattachées à des entités du jeu (`0xf00dxxxx`), pas directement à des pseudos.
Le script déduit qui est qui :

1. la défense est présente pendant la préparation, l'attaque apparaît ensuite ;
2. une victime, c'est la trajectoire qui s'arrête juste après son kill ;
3. le joueur qui a enregistré le replay, s'il survit, c'est la première entité de son équipe à apparaître :
   son propre jeu connaît son personnage avant ceux des autres (vérifié sur R02, R03 et R09) ;
4. un autre survivant, c'est l'entité la plus proche de ses victimes et tournée vers elles au moment de ses
   kills, sinon par élimination. Quand c'est serré, `resume.txt` l'indique avec « INCERTAIN ».

La méthode utilisée pour chaque joueur est écrite dans `resume.txt`. Si une attribution est fausse :

```
python r6_replay_tool.py Match-XXXX-R09.rec --noms "0xf00d20d7=Decoy_99,0xf00d209b=nitramossur1"
```

## Format du fichier (ce qu'on en sait)

Déduit d'un replay de la saison `Y11S3_Alpha04` :

- **En-tête** `dissect` : paires `<u64 longueur><clé><u64 longueur><valeur>` (joueurs, opérateurs, équipes,
  carte `worldid`, numéro de manche, scores...).
- **Corps** : frames zstd. Une fois décompressé, le flux contient plusieurs **canaux** écrits l'un après
  l'autre. Chacun couvre toute la manche et se découpe en **images** (ticks, ~9,5 ms) :
  `[u32 numéro d'image][u32 taille][u32 0][données]`. Le numéro d'image est commun à tous les canaux :
  c'est la base de temps.
- **Horloge** : `18 37 46 6C 04` + `u32` millisecondes restantes dans la phase (préparation, action,
  désamorceur posé). L'horloge remonte à chaque nouvelle phase et retombe à 0 quand la manche se termine.
- **Fil d'événements** : `59 34 E5 8B 04 ...` ; le tueur suit `22 D9 13 3C BA`, la victime suit
  `22 AC 19 0F 70`, et les messages (« X a trouvé les bombes ») suivent `26 E3 09 00 79 00 00 00 00`.
- **Positions** :
  - canal principal : paquets de propriété `<entité u32><0 u32><champ u32> 60 73 85 FE <sous-type u16> <x y z float32>` ;
  - canal des positions : `<entité u32><0 u32> <x y z float32> <0 u32>` ;
  - certains objets de ce même canal sont indexés par une adresse mémoire 64 bits au lieu d'une entité.
- **Orientation** (même canal, même format d'enregistrement) :
  - lacet du corps : `<entité u32><0 u32> <x y 0 float32> <0 u32>`, vecteur unitaire horizontal ;
  - caméra : vecteur unitaire 3D `<x y z>` indexé par une adresse mémoire ; `asin(z)` donne le tangage.
    Une caméra est rattachée au joueur dont le lacet colle à sa direction horizontale.
  - Vérifié sur le replay de test : au moment de chaque kill, le regard du tueur pointe vers sa victime
    à 0–3° près, et un tir à travers le plancher apparaît bien avec un tangage vers le bas.

## Limites

- Le replay est enregistré du point de vue d'un joueur. Le serveur n'envoie pas toujours la position
  des adversaires, et le jeu n'envoie rien quand un joueur ne bouge pas. Un trou qui commence et finit
  au même endroit est traité comme de l'immobilité. Les autres trous sont affichés comme
  « position inconnue » (cercle vide dans le lecteur).
- Le tangage n'est disponible que si la caméra du joueur est dans le replay (6 joueurs sur 7 dans le
  replay de test). Sinon, seul le lacet est connu.
- Il n'y a pas de plan de la carte : le lecteur dessine en fond l'ensemble des positions de tous les
  joueurs, ce qui dessine la forme du bâtiment.
- Testé sur trois manches d'un même match (R02, R03 et R09). Les signatures d'octets peuvent changer d'une saison à l'autre.

Les identifiants de cartes viennent du projet [r6-dissect](https://github.com/redraskal/r6-dissect).
