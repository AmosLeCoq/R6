# R6 — reconstitution de manche à partir d'un replay Rainbow Six Siege

`r6_replay_tool.py` lit un replay `.rec` de Rainbow Six Siege et reconstitue la manche entière :
qui est où, à quel moment, et ce qui se passe (kills, messages, fin de manche).

```
pip install -r requirements.txt
python r6_replay_tool.py Match-XXXX-R09.rec
```

Les replays se trouvent dans le dossier du jeu, sous `MatchReplay/<match>/Match-...-RXX.rec` (un fichier par manche).

## Sorties

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
