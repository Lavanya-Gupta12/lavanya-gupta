## 01 The visit: The technician has one hour at one station next week. Which station, what exactly do they do,and what do they check first? Why that station and not the second-best one?

The technician has one hour at one station, so I would send them to S04
(Foothills Community Garden).

S04 has two problems. First, on sunny afternoons its temperature reads too warm
compared with the other stations, and the gap keeps growing week by week. At night
it is normal. That points to the radiation shield not letting air flow through,
so the sun heats the sensor. The likely cause is the repaint on 15 March — paint may
be blocking the shield slats. Second, the rain gauge was blocked by leaves. It missed
the 29–30 March storm and then dumped 33 mm when it was cleaned.

What they should do, in order:
1. First, look at the shield slats for paint or blockage. This tests the main suspect.
2. Hold a hand thermometer in the shade next to the shield and note both readings.
   This proves whether the station really reads too warm.
3. Clean or replace the shield.
4. Fit a leaf guard on the rain gauge, pour in a measured cup of water and count the
   tips to check it works.

Why not S03, the runner-up? Its temperature sensor is also failing, but in an obvious
way — values like -40 and 85 °C and a reading stuck at 6.5 °C for 80 hours. check.py
catches and removes these automatically, and only about 3.5 days are lost. A volunteer
can check the sensor's plug. S04's error is worse because it is quiet: the numbers look
believable, so they would sneak into the climate summary as fake warm afternoons, and
software cannot fix them.

The other stations do not need a visit: S06 needs its clock setting changed, S07 its
wind unit setting, S02 a calibration kit, S08 a SIM top-up, and S05's humidity fixed
itself.

## 02 My least certain call: Pick the decision in flags.csv you are least sure about. What is the evidenceeach way, and what single piece of information would settle it?

My least certain call is the S04 daytime temperature bias (excluding daytime readings
from 26 March, because those shouldn't be included in the spring summary until they are first checked to be wrong or right).

Evidence that it is a real fault:
- The error only happens in the daytime; nights are fine. A shield that does not let
  air through only goes wrong when the sun is shining on it.
- Only S04 shows it, so it is probably S04's equipment, not the weather.
- It started after the post was repainted on 15 March.

Evidence against:
- The error grows slowly, week by week. If paint had blocked the slats on 15 March,
  I would expect a sudden jump on that day, not a slow climb.
- The start date (26 March) is only an estimate from a 7-day average.
- S04 is in a garden, so the warmth could be real — for example soil drying out or
  walls heating up in spring sun.
- The note says the paint is white, and white paint alone should not make the sensor
  warmer — only paint blocking the slats would.

What would settle it: one side-by-side reading. The technician holds a hand
thermometer in the shade next to the shield on a sunny afternoon and compares it with
the station's reading at the same time. If they match, the shield is fine and the
warmth is real. If the station reads higher, the shield is faulty.

## 03 Where the AI was wrong: Give one or two moments where an AI tool gave you something plausible butwrong (code, an explanation, a threshold). Quote it briefly and say how you noticed.

1. S06 clock. ChatGPT's first check.py said S06 had 36 duplicate rows and a
   6-hour gap on 24 March. That was wrong. After the battery swap, the logger
   restarted with its clock 6 hours behind (local time instead of UTC). So new
   readings were saved with old times — for example, the 16:10 reading was labelled
   10:10. The code thought these were duplicates and deleted them. Really, their
   time labels just needed +6 hours. I noticed because the time in S06.csv jumps
   backwards (16:00, then 10:10), the maintenance log says the logger rebooted that
   day, and the GPS line written at logger time 11:00 says the real time was 17:00.
   The fix: correct the clock first, then look for duplicates.

2. S02 pressure. In the report, ChatGPT said to add 6.7 hPa to S02's pressure.
   The correct fix is to subtract 6.7. S02 is 100 m higher than S03, so its pressure
   should be about 10 hPa lower. The old board was right (10.3 lower), but the new
   board is only 3.4 lower, so it reads 6.7 too high. Adding would have made it worse.

## 04 The same checker has to run unattended on 500 stations, and nobody will read every flag.What do you change, what do you drop, and what would you refuse to automate?

Change:
- Compare each station only with its k nearest neighbours (by distance and
  altitude), not with the whole network. Stations far apart have different weather.
- Rank flags by how serious they are, and only alert when a problem is new or
  getting worse. Don't repeat the same alert every day for a known problem.
- Set thresholds by testing on data we know is good, and adjust them so false
  alarms are rare. With 500 stations, even a few false alarms each would mean
  hundreds of useless flags, and people would stop reading them.
- Turn the maintenance log into a simple form (station, date, what was done) so
  the checker can read it, instead of free text.
- Make the code faster — it already takes about 1 minute for 8 stations.

Drop:
- The long written note on every flag; use a short issue code and a severity level.
- One-off rules written just for this pack.

Refuse to automate:
- Applying corrections to the official climate record without a person approving
  them. A correction can be wrong — for S02, the teacher thought the old pressure
  board was wrong, but the data showed it was the new one.
- Deleting raw data. Always keep the original and store corrections separately, so
  any mistake can be checked and undone.
- Choosing where the technician goes. That needs local knowledge and judgement.

## 05 By hand: solar noon at S04 on 20 March 2025

S04's longitude from stations.csv is -105.146, which means 105.146 degrees west
of Greenwich.

The Earth turns 360 degrees in 24 hours, so 15 degrees per hour, or 4 minutes per
degree. At Greenwich (0 degrees) the sun is highest at about 12:00 UTC.

105.146 x 4 = 420.6 minutes = about 7 hours 1 minute.
So the sun reaches its highest point at S04 about 7 hours after Greenwich:
12:00 + 7:01 = about 19:01 UTC.

Why this is useful: it is a way to check a station's clock without GPS. Sunlight
should peak around 19:08 UTC at S04. If a station's sunlight peaks at a very
different time, its clock is wrong. That is what happened at S06: after 24 March
its sunlight peaked about 6 hours early in the file, matching its clock being
6 hours behind.