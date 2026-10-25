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

1. S06 clock. ChatGPT's first check.py flagged 36 "conflicting duplicate
   timestamps" and a "370-minute gap" at S06. That was wrong. After the battery
   swap on 24 March, the logger restarted on local time (UTC-6), so its clock went
   6 hours behind: the times go 16:00 and then back to 10:10. The hours 10:10-16:00
   appear twice, but the second set are new real readings with the wrong time, not
   duplicates. The code checked for duplicates before fixing the clock, so it deleted
   them and never found the clock error. I noticed because the maintenance log says
   the logger rebooted on 24 March, S06.csv jumps backwards in time, and the GPS
   line at logger time 11:00 says the real time is 17:00. The fix was to correct the
   clock first, then check duplicates.

2. S02 pressure sign. In the report, ChatGPT wrote "add 6.7 hPa" for S02. It
   dropped the minus sign from "add -6.7" in flags.csv. The correct fix is to
   subtract 6.7. I checked with elevation: S02 is 100 m above S03, so it should read
   about 10 hPa lower. The old board was 10.3 lower (correct); the new board is only
   3.4 lower, so it reads 6.7 too high. Adding 6.7 would have doubled the error.

   

