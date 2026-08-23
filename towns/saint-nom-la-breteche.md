# Saint-Nom-la-Bretèche (78860)

Village très aisé de la plaine de Versailles, adossé à la **forêt de Marly** et organisé autour du **golf de Saint-Nom-la-Bretèche**, l'un des plus cotés d'Île-de-France. Densité de 421 hab/km² — c'est un **village**, pas une commune pavillonnaire dense. Desservi par la **ligne L** (Saint-Lazare direct) et depuis 2022 par le **tram T13** à la même gare. **Point de vigilance : la desserte est bonne vers Paris-Saint-Lazare mais faible vers le sud (Issy), et le profil village très peu dense est le profil à cambriolages de ce fichier.**

## Repères
- **Population**: ~4 965 hab.
- **Densité**: ~421 hab/km²
- **Prix maison (2025)**: ~5 200–5 600 €/m²
- **Prix maison DVF (2023-25)**: **5 109 €/m²** (médiane, n=118)
- **~700 k€ achète**: ~137 m²
- **Terrain maison (indicatif)**: grandes parcelles, ~500–1 200 m²
- **Terrain médian (DVF)**: 776 m² (médiane des ventes)
- **Revenu médian (INSEE)**: ~44 000 €/an — parmi les plus élevés du fichier
- **Propriétaires**: ~82 %
- **Part de maisons**: ~78 %
- **Logements 4 pièces et +**: ~82 %
- **Standing (chic)**: ★★★★★ — golf, forêt de Marly, plaine de Versailles ; l'adresse la plus cossue du secteur avec Marnes-la-Coquette
- **Transport**: gare **Saint-Nom-la-Bretèche – Forêt de Marly** — **Transilien L** (→ Paris Saint-Lazare) + **tram T13** (→ Saint-Germain RER A / Saint-Cyr RER C)
- **Vers La Défense**: ~40–50 min (L → Saint-Lazare avec correspondance, ou T13 → RER A)
- **Vers Paris**: ~35–45 min (ligne L directe → Saint-Lazare)
- **Vers Issy-les-Moulineaux**: ~55–70 min — **deux correspondances**, le point noir de la commune
- **Facilité A/R Paris**: bonne vers Saint-Lazare (direct), moyenne ailleurs
- **Vélo**: bon en loisir (plaine de Versailles, forêt de Marly) ; **aucun axe cyclable utile vers Paris**
- **Couloir aérien**: survols d'aviation légère possibles (aérodrome de Saint-Cyr, comme Noisy/Bailly) — à vérifier par quartier
- **Topographie**: plateau de la plaine de Versailles, **globalement plat**
- **Logement social**: très faible — **commune carencée SRU**, donc pression future à construire
- **Cambriolages (‰ logements)**: 13,16 ‰
- **Violences (‰ hab.)**: 0,00 ‰
- **Dégradations (‰ hab.)**: 6,65 ‰
- **Contraintes patrimoniales**: **fortes** — village ancien, abords de monuments et proximité du domaine de Versailles ; **à vérifier parcelle par parcelle, c'est le filtre éliminatoire de l'acheteur**
- **Quartiers (cibler / éviter)**: cibler le **pavillonnaire côté golf et lisière de forêt** ; **éviter le cœur de village ancien** (verrouillage ABF) et les abords immédiats de la RD 307
- **Caractère**: village-golf, très résidentiel, calme, peu de commerces au regard du niveau de vie

## Structure de la population
- Commune **âgée et installée** : forte part de 60 ans et plus, peu de jeunes ménages.
- CSP dominante : **cadres et professions supérieures**, puis retraités.
- Ménages très majoritairement propriétaires de maisons individuelles.

## Points forts
- **Ligne L directe vers Saint-Lazare** + T13 pour rejoindre le RER A — deux réseaux sur la même gare.
- **Terrain plat et grandes parcelles** (**terrain médian DVF 776 m²**) — le meilleur profil terrain du fichier.
- Cadre exceptionnel : forêt de Marly, golf, plaine de Versailles ; standing et liquidité à la revente.
- **€/m² plus bas que la réputation ne le laisse croire** (médiane DVF **5 109 €/m²**, à peine au-dessus de Noisy-le-Roi et sous Fourqueux).

## Points faibles
- **Vers Issy : ~55–70 min avec deux correspondances** — rédhibitoire si le travail est au sud.
- **Verrouillage patrimonial probable sur le cœur ancien** — le filtre éliminatoire ; ne pas s'engager sans vérifier les servitudes AC1 de la parcelle.
- **Cambriolages 13,16 ‰** — mesuré, et conforme à la corrélation inverse densité/cambriolages du fichier (Mareil-Marly 20,7 ‰, Bailly 13,4 ‰). C'est **plus du double de Noisy-le-Roi** (6,01 ‰) pour un cadre comparable.
- **Commune carencée SRU** → programmes de logements à venir, potentiellement près de chez vous.
- Peu de commerces et de services de proximité ; tout se fait en voiture.

## Pour notre projet
**À regarder sérieusement pour le terrain et le calme, à condition de trancher deux points.** Le prix médian des ventes est de ~809 k€, au-dessus du budget, mais la médiane à **5 109 €/m²** dit qu'on peut y trouver du 140–150 m² autour de 750 k€ — soit le même €/m² que Noisy-le-Roi (5 067) pour un cadre supérieur et un **terrain médian de 776 m² contre 578**.

⚠️ Les violences mesurées à **0,00 ‰** ne veulent pas dire « zéro » : sur 4 965 habitants, un taux ‰ est volatil et un seul fait le ferait bondir. Le chiffre solide ici est le cambriolage.

Les deux réserves sont dirimantes si elles tombent mal : **le trajet vers Issy** (deux correspondances) et **le verrouillage patrimonial du cœur de village**. Cibler exclusivement le pavillonnaire côté golf / lisière de forêt, et faire tourner `scripts/enrich.py` sur toute parcelle candidate avant d'aller plus loin.

## Sources
INSEE / geo.api.gouv.fr (population, superficie) ; **geo-DVF 2023-2025** pour les prix et terrains mesurés ; Île-de-France Mobilités (ligne L, T13) ; loi SRU. Les lignes « Prix maison DVF », « Terrain médian (DVF) » et les trois lignes de délinquance sont régénérées par `scripts/enrich_towns.py`.
