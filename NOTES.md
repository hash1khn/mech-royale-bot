# Mech Royale bot

Python bot for the Builders Mech Royale track (mechfight/1). Entry: `bot.py`, wrapper: `mechfight.py` (from the kit).

## Score log

| tag | change | practice score |
| --- | --- | --- |
| v1 (f614445) | map + zone + lead-aim duels + retreat + loot | 3.60 |
| v3 (0be1ff6) | recover mode, lightning close-in, direct laser aim | 3.93 |
| v4 | kite at ~140 m (house bots hit ~15% past 120 m, ~80% under 80 m), latency-aware lead, laser > lightning, gun upgrades, 360 scan | pending |

## Replay findings (mechanics-v2-16 sample)

House-bot gun hit rate by range: <80 m 65-90%, 80-120 m ~30%, 120-160 m ~15%, >160 m ~10%.
A linear lead (0.8 x velocity, +0.075 s latency) scored against their recorded movement: 80% at 90-120 m, 70% at 120-150 m, 60% at 150-180 m.
