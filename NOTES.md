# Mech Royale bot

Python bot for the Builders Mech Royale track (mechfight/1). Entry: `bot.py`, wrapper: `mechfight.py` (from the kit).

## Score log

| tag | change | practice score |
| --- | --- | --- |
| v1 (f614445) | map + zone + lead-aim duels + retreat + loot | 3.60 |
| v3 (0be1ff6) | recover mode, lightning close-in, direct laser aim | 3.93 |
| v4 | kite at ~140 m (house bots hit ~15% past 120 m, ~80% under 80 m), latency-aware lead, laser > lightning, gun upgrades, 360 scan | 4.09 |
| v5 | steer around walls while kiting, remember kill sites for cubes, fetch a nearby laser mid-fight (sim: 4.5 -> 5.9) | 4.24 |
| v6a | farming: crates/drones for cubes, supply drops, cube pickup mid-fight; range 140 | 5.00 |
| v6b | v6a at range 75/110 | 4.84 |
| v6c | v6b + seek last seen enemy | 4.63 |
| v6d | v6a, ignore mechs beyond 120 m, range 100/130, cube radius 150 | 5.11 |
| v7 | v6d + farming director (drone memory, commitment, stand-off, give-up list, travel boost): local cube income +60% | pending |

## Replay findings (mechanics-v2-16 sample)

House-bot gun hit rate by range: <80 m 65-90%, 80-120 m ~30%, 120-160 m ~15%, >160 m ~10%.
A linear lead (0.8 x velocity, +0.075 s latency) scored against their recorded movement: 80% at 90-120 m, 70% at 120-150 m, 60% at 150-180 m.

## Local simulator

`python3 tools/sim.py --n 100` plays rough local matches (real map, zone, weapons, scripted squad of hunters). It is not the real server: use it to find bugs and A/B changes, then confirm with a submission. Needs `check/` and `arena/` from the kit next to the repo files.
