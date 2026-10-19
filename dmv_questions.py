"""DMV practice-test question banks, keyed by state.

Static content (no DB) — a question bank is reference data, not user data, so it
lives in code like a catalog rather than in SQLite. Structured as {state_code:
{"name", "meta", "questions"}} so more states drop in beside NC without touching the
routes or UI: get_states() and get_quiz() stay state-agnostic.

Questions are practice material paraphrased from each state's official driver
handbook (NC: the NCDMV Driver's Handbook) — well-established road rules and
state-specific limits, not copied verbatim from any exam. Always a practice aid, never
a substitute for the official handbook, and the UI says so.

Each question: {"q", "options" [list], "answer" [index into options], "explain"}.
"""

from __future__ import annotations

import random

_NC_QUESTIONS = [
    {
        "q": "Unless otherwise posted, the maximum speed limit on rural interstate highways in North Carolina is:",
        "options": ["55 mph", "65 mph", "70 mph", "75 mph"],
        "answer": 2,
        "explain": "NC's statutory maximum on rural interstates is 70 mph unless a different limit is posted.",
    },
    {
        "q": "Unless otherwise posted, the speed limit inside a North Carolina city or town is:",
        "options": ["25 mph", "30 mph", "35 mph", "45 mph"],
        "answer": 2,
        "explain": "Inside municipal limits the default speed limit is 35 mph unless signs say otherwise.",
    },
    {
        "q": "For drivers age 21 and older, it is illegal to drive with a blood alcohol concentration (BAC) of:",
        "options": ["0.05 or higher", "0.08 or higher", "0.10 or higher", "0.12 or higher"],
        "answer": 1,
        "explain": "The legal limit for drivers 21 and over is 0.08. You can still be charged below that if impaired.",
    },
    {
        "q": "Under North Carolina's zero-tolerance law, a driver under age 21 may drive with a BAC of no more than:",
        "options": ["0.00", "0.02", "0.04", "0.08"],
        "answer": 0,
        "explain": "Drivers under 21 may not have any measurable alcohol — the limit is 0.00.",
    },
    {
        "q": "The BAC limit for a driver operating a commercial vehicle is:",
        "options": ["0.00", "0.04", "0.08", "0.10"],
        "answer": 1,
        "explain": "Commercial drivers are held to a stricter 0.04 limit while operating a commercial vehicle.",
    },
    {
        "q": "Accumulating how many driver-license points within a three-year period may lead to a suspension in NC?",
        "options": ["7 points", "10 points", "12 points", "15 points"],
        "answer": 2,
        "explain": "Twelve points within three years can result in a license suspension.",
    },
    {
        "q": "According to the NC Driver's Handbook, the minimum following distance in normal weather and traffic is:",
        "options": ["A one-second gap", "A two-second gap", "A three-second gap", "One car length"],
        "answer": 1,
        "explain": "NC teaches the two-second rule: pick a fixed point and make sure at least two seconds pass before you reach it. Increase it in bad weather or heavy traffic.",
    },
    {
        "q": "A steady red traffic signal means:",
        "options": [
            "Stop; you may turn right after a full stop unless a sign prohibits it",
            "Slow down and proceed if the way is clear",
            "Stop only if other traffic is present",
            "The signal is broken; treat it as a yield",
        ],
        "answer": 0,
        "explain": "Come to a complete stop. A right turn on red is allowed after stopping and yielding, unless posted 'No Turn on Red'.",
    },
    {
        "q": "A flashing red traffic light means:",
        "options": [
            "Speed up to clear the intersection",
            "Stop, then proceed when it is safe",
            "Proceed without stopping",
            "The light is out of order — do not enter",
        ],
        "answer": 1,
        "explain": "Treat a flashing red exactly like a stop sign: come to a full stop, then go when it is safe.",
    },
    {
        "q": "A flashing yellow traffic light means:",
        "options": [
            "Stop and wait for a green light",
            "Slow down and proceed with caution",
            "Come to a complete stop",
            "Traffic ahead must stop for you",
        ],
        "answer": 1,
        "explain": "A flashing yellow means slow down and proceed carefully, watching for cross traffic.",
    },
    {
        "q": "On a two-lane road, when a stopped school bus has its red lights flashing and stop arm extended, you must:",
        "options": [
            "Stop only if you are behind the bus",
            "Slow to 20 mph and pass carefully",
            "Stop whether you are behind or approaching the bus",
            "Proceed if no children are visible",
        ],
        "answer": 2,
        "explain": "On a two-lane road, traffic in BOTH directions must stop until the bus withdraws its stop arm and turns off its red lights.",
    },
    {
        "q": "A solid yellow line on your side of the center line means:",
        "options": [
            "Passing is allowed if the road is clear",
            "Passing is not allowed",
            "The lane is ending",
            "You may only pass on the right",
        ],
        "answer": 1,
        "explain": "A solid yellow line on your side means you may not cross it to pass.",
    },
    {
        "q": "Two vehicles arrive at a four-way stop at exactly the same time. Who has the right of way?",
        "options": [
            "The driver on the left",
            "The driver on the right",
            "The larger vehicle",
            "Whoever accelerates first",
        ],
        "answer": 1,
        "explain": "When two vehicles reach a four-way stop together, the driver on the left yields to the driver on the right.",
    },
    {
        "q": "When approaching a roundabout, you should:",
        "options": [
            "Stop and wait for a green light",
            "Yield to traffic already in the circle, then enter when there is a gap",
            "Enter quickly before other cars",
            "Come to a full stop before entering, always",
        ],
        "answer": 1,
        "explain": "Traffic already in the roundabout has the right of way. Yield, then merge into a gap and travel counterclockwise.",
    },
    {
        "q": "If the rear wheels of your vehicle begin to skid, you should:",
        "options": [
            "Brake hard and hold the wheel straight",
            "Steer in the direction you want the front of the car to go",
            "Steer in the opposite direction of the skid",
            "Accelerate to regain traction",
        ],
        "answer": 1,
        "explain": "Ease off the gas and steer in the direction you want the front to go ('steer into the skid'); avoid slamming the brakes.",
    },
    {
        "q": "To reduce the risk of hydroplaning on a wet road, you should:",
        "options": [
            "Increase your speed to cut through the water",
            "Slow down and avoid hard braking",
            "Brake firmly and steer sharply",
            "Turn on your high beams",
        ],
        "answer": 1,
        "explain": "Hydroplaning risk rises with speed. Slow down, ease off the accelerator, and avoid sudden braking or steering.",
    },
    {
        "q": "In North Carolina, you are required to turn on your headlights:",
        "options": [
            "Only when it is completely dark",
            "From sunset to sunrise, and whenever weather requires your windshield wipers",
            "Only on interstate highways",
            "Only in heavy fog",
        ],
        "answer": 1,
        "explain": "Headlights are required from sunset to sunrise and any time you must use your wipers because of rain, snow, or fog.",
    },
    {
        "q": "North Carolina's 'Move Over' law requires that when you approach a stopped emergency or utility vehicle with its lights flashing, you:",
        "options": [
            "Maintain your speed and lane",
            "Move over a lane if you safely can, or slow down",
            "Stop completely until it leaves",
            "Sound your horn as a warning",
        ],
        "answer": 1,
        "explain": "Move over into a farther lane when it is safe; if you cannot change lanes, slow down and proceed with caution.",
    },
    {
        "q": "Seat belt use in North Carolina is:",
        "options": [
            "Required only for the driver",
            "Required for front-seat occupants only",
            "Required for the driver and all passengers",
            "Optional for adults in the back seat",
        ],
        "answer": 2,
        "explain": "NC law requires the driver and all passengers to be buckled, and it is enforced as a primary offense.",
    },
    {
        "q": "A pedestrian is crossing in a marked crosswalk ahead of you. You should:",
        "options": [
            "Honk so they hurry",
            "Yield and let the pedestrian finish crossing",
            "Proceed if you can go around them",
            "Continue — vehicles always have the right of way",
        ],
        "answer": 1,
        "explain": "Yield to pedestrians in a crosswalk. They have the right of way; wait until they have safely crossed.",
    },
    {
        "q": "When entering a freeway from an on-ramp, you should:",
        "options": [
            "Stop at the end of the ramp and wait for a large gap",
            "Enter slowly and let traffic adjust to you",
            "Use the ramp to match the speed of traffic, then merge into a gap",
            "Merge immediately regardless of traffic",
        ],
        "answer": 2,
        "explain": "Use the acceleration lane to reach the speed of freeway traffic, then merge smoothly into an opening.",
    },
    {
        "q": "Alcohol is best described as:",
        "options": [
            "A stimulant that sharpens reflexes",
            "A depressant that slows reactions and impairs judgment",
            "Harmless in small amounts while driving",
            "A substance that only affects vision",
        ],
        "answer": 1,
        "explain": "Alcohol is a depressant. It slows reaction time and impairs judgment — often the first ability affected.",
    },
    {
        "q": "Another vehicle is passing you. You should:",
        "options": [
            "Speed up so they cannot pass",
            "Maintain your speed and stay in your lane",
            "Brake hard to let them in",
            "Move onto the shoulder",
        ],
        "answer": 1,
        "explain": "Hold your speed and lane position so the other driver can complete the pass safely.",
    },
    {
        "q": "If you miss your exit on a freeway, you should:",
        "options": [
            "Back up on the shoulder to reach it",
            "Stop and wait for a gap to reverse",
            "Continue to the next exit and turn around there",
            "Make a U-turn across the median",
        ],
        "answer": 2,
        "explain": "Never stop or back up on a freeway. Go on to the next exit and find a safe, legal way back.",
    },
    {
        "q": "At a railroad crossing with flashing lights and lowered gates, you must:",
        "options": [
            "Drive around the gates if no train is visible",
            "Stop and wait until the gates rise and the lights stop",
            "Proceed slowly through the crossing",
            "Stop only if you hear a train horn",
        ],
        "answer": 1,
        "explain": "Stop and wait. Never drive around lowered gates — a second train may be coming, and gates rise only when it is safe.",
    },
    # --- Researched additions (2026-07-24): NC-specific numbers and commonly-missed
    # rules verified against the NCDOT Driver's Handbook and reputable practice-test
    # sources. These are the ones people most often get wrong.
    {
        "q": "You must give a continuous turn signal for at least how far before you turn?",
        "options": ["50 feet", "100 feet", "200 feet", "300 feet"],
        "answer": 1,
        "explain": "NC requires signaling continuously for at least 100 feet before turning so other drivers can react.",
    },
    {
        "q": "You may NOT park within how many feet of a fire hydrant?",
        "options": ["10 feet", "15 feet", "20 feet", "25 feet"],
        "answer": 1,
        "explain": "Parking within 15 feet of a fire hydrant is prohibited — fire crews may need to reach it in an emergency.",
    },
    {
        "q": "You may NOT park within how many feet of a stop sign, yield sign, or traffic signal?",
        "options": ["15 feet", "20 feet", "30 feet", "50 feet"],
        "answer": 2,
        "explain": "Parking is prohibited within 30 feet of a stop sign, yield sign, or traffic signal so it stays visible.",
    },
    {
        "q": "When making a left turn at an intersection, you must:",
        "options": [
            "Turn first, since left turns have priority",
            "Yield to oncoming traffic and to pedestrians",
            "Yield only to vehicles on your right",
            "Sound your horn and proceed",
        ],
        "answer": 1,
        "explain": "A left-turning driver must yield to oncoming traffic and to pedestrians. This is one of the most commonly missed rules.",
    },
    {
        "q": "A school bus stops with red lights flashing on the OTHER side of a highway divided by a median. You are traveling in the opposite direction. You must:",
        "options": [
            "Stop and wait like everyone else",
            "Continue with caution — on a median-divided highway, only traffic behind the bus must stop",
            "Stop only if you see children",
            "Slow to 20 mph but keep moving",
        ],
        "answer": 1,
        "explain": "On a highway separated by a median, only vehicles traveling the same direction as the bus must stop. WARNING: on a road with NO median, ALL traffic in both directions must stop.",
    },
    {
        "q": "A Level 2 (limited provisional) license holder generally may not drive unsupervised between:",
        "options": [
            "Midnight and 6 a.m.",
            "9 p.m. and 5 a.m.",
            "10 p.m. and 4 a.m.",
            "11 p.m. and 6 a.m.",
        ],
        "answer": 1,
        "explain": "Level 2 drivers may not drive unsupervised between 9 p.m. and 5 a.m., with limited exceptions such as driving to or from work.",
    },
    {
        "q": "Leaving the scene of a crash that caused injury or death is:",
        "options": [
            "A minor traffic infraction",
            "A felony punishable by prison time",
            "Only a fine if reported later",
            "Not an offense if you were not at fault",
        ],
        "answer": 1,
        "explain": "Hit-and-run involving injury or death is a felony in NC (a Class F felony for serious injury or death), punishable by imprisonment.",
    },
    {
        "q": "Which of the following must come to a stop at ALL railroad crossings?",
        "options": [
            "Every passenger car",
            "School buses and vehicles carrying hazardous materials",
            "Motorcycles only",
            "Nobody, unless a train is visible",
        ],
        "answer": 1,
        "explain": "School/activity buses, vehicles carrying passengers for hire, vehicles seating 16+ people, and hazmat carriers must stop at every railroad crossing.",
    },
    {
        "q": "In North Carolina, a child must ride in a proper child restraint (car seat or booster) if they are:",
        "options": [
            "Under 5 years old",
            "Under 8 years old AND less than 80 pounds",
            "Under 40 pounds only",
            "Under 12 years old, regardless of weight",
        ],
        "answer": 1,
        "explain": "NC law requires a weight-appropriate child restraint for any child under age 8 and less than 80 pounds.",
    },
    {
        "q": "You must dim your high-beam headlights when an oncoming vehicle is within:",
        "options": ["200 feet", "500 feet", "1,000 feet", "100 feet"],
        "answer": 1,
        "explain": "Dim your high beams within 500 feet of an oncoming vehicle, and within 200 feet of a vehicle you are following.",
    },
    {
        "q": "You may NOT pass another vehicle when you are within 100 feet of:",
        "options": [
            "A parked car",
            "An intersection or railroad crossing",
            "A driveway",
            "A bus stop",
        ],
        "answer": 1,
        "explain": "Passing is prohibited within 100 feet of an intersection or railroad crossing, and anywhere you can't see far enough ahead (hills, curves).",
    },
    {
        "q": "In North Carolina, a driver under age 18 may:",
        "options": [
            "Use a hands-free phone only",
            "Not use a mobile phone while driving at all",
            "Text only while stopped at a red light",
            "Use any phone freely",
        ],
        "answer": 1,
        "explain": "Drivers under 18 may not use a mobile phone while driving (except to call a parent or 911). Texting while driving is illegal for ALL NC drivers.",
    },
    {
        "q": "Before crossing railroad tracks after one train has passed, you should:",
        "options": [
            "Cross immediately — the danger is over",
            "Make sure no other train is approaching from either direction",
            "Wait a full five minutes",
            "Sound your horn and proceed",
        ],
        "answer": 1,
        "explain": "A second train may be coming on another track from either direction. Always look both ways before crossing.",
    },
    # --- Second expansion (2026-07-24): signs, markings, GDL licensing, sharing the
    # road, adverse conditions, and penalties — broadening the pool so a 25-question
    # test rarely repeats, all verified against the NCDMV Driver's Handbook.
    {
        "q": "An eight-sided (octagonal) sign always means:",
        "options": ["Yield", "Stop", "Do not enter", "Warning"],
        "answer": 1,
        "explain": "Only stop signs are octagonal. The unique eight-sided shape lets you recognize it even when snow or dirt hides the letters.",
    },
    {
        "q": "A downward-pointing triangle sign means:",
        "options": ["Stop", "Yield", "No passing", "Merge"],
        "answer": 1,
        "explain": "An inverted-triangle sign means yield: slow down, be ready to stop, and give the right of way to traffic and pedestrians.",
    },
    {
        "q": "A diamond-shaped sign is used to:",
        "options": [
            "Give directions to a city",
            "Warn of hazards or changing road conditions ahead",
            "Mark a hospital",
            "Show a speed limit",
        ],
        "answer": 1,
        "explain": "Diamond-shaped signs are warning signs — they alert you to curves, merges, pedestrian crossings, and other conditions ahead.",
    },
    {
        "q": "Orange traffic signs indicate:",
        "options": [
            "A construction or work zone",
            "A recreational area",
            "A motorist service like fuel or food",
            "A school zone",
        ],
        "answer": 0,
        "explain": "Orange signs (and cones) mark construction and maintenance work zones. Slow down — traffic fines are often doubled in work zones.",
    },
    {
        "q": "A five-sided (pentagon) sign usually marks:",
        "options": [
            "A no-passing zone",
            "A school zone or school crossing",
            "A hospital",
            "A dead-end street",
        ],
        "answer": 1,
        "explain": "The pentagon shape is used for school-zone and school-crossing signs. Watch for children and be ready to stop.",
    },
    {
        "q": "A broken (dashed) white line between lanes means:",
        "options": [
            "Passing is prohibited",
            "Lanes go the same direction and you may change lanes when safe",
            "The road is one-way",
            "You must stay in your lane",
        ],
        "answer": 1,
        "explain": "A dashed white line separates lanes of traffic moving the same direction; you may change lanes across it when it's safe.",
    },
    {
        "q": "Two solid yellow lines in the center of the road mean:",
        "options": [
            "Passing is allowed in both directions",
            "No passing in either direction",
            "A center turn lane",
            "The road is closed",
        ],
        "answer": 1,
        "explain": "Double solid yellow lines mean neither direction may cross to pass. You may cross only to turn into or out of a driveway or road.",
    },
    {
        "q": "When parking uphill next to a curb, you should turn your front wheels:",
        "options": [
            "Toward the curb",
            "Away from the curb (toward the street)",
            "Straight ahead",
            "It doesn't matter",
        ],
        "answer": 1,
        "explain": "Uphill with a curb, turn the wheels away from the curb and let the car roll back slightly so a rear tire rests against the curb.",
    },
    {
        "q": "When parking downhill next to a curb, you should turn your front wheels:",
        "options": [
            "Toward the curb",
            "Away from the curb",
            "Straight ahead",
            "Only set the parking brake",
        ],
        "answer": 0,
        "explain": "Downhill (or uphill with no curb), turn the wheels toward the curb/edge so a runaway car rolls off the road, not into traffic.",
    },
    {
        "q": "To signal a left turn with your arm, you should extend it:",
        "options": [
            "Straight out to the left",
            "Out and bent upward",
            "Out and bent downward",
            "Straight up",
        ],
        "answer": 0,
        "explain": "Left-turn hand signal: arm straight out. Bent upward means a right turn; bent downward means slowing or stopping.",
    },
    {
        "q": "The safest way to check your blind spot before changing lanes is to:",
        "options": [
            "Rely only on your side mirror",
            "Glance over your shoulder in the direction you're moving",
            "Use only the rearview mirror",
            "Honk before moving over",
        ],
        "answer": 1,
        "explain": "Mirrors leave blind spots. A quick glance over your shoulder is the only reliable way to see a vehicle beside you before merging.",
    },
    {
        "q": "If you have anti-lock brakes (ABS) and need to stop quickly, you should:",
        "options": [
            "Pump the brake pedal rapidly",
            "Apply firm, steady pressure and keep steering",
            "Press lightly and coast",
            "Use the parking brake instead",
        ],
        "answer": 1,
        "explain": "With ABS, hold firm, steady pressure — the system pumps for you — and steer around the hazard. Do not pump ABS brakes.",
    },
    {
        "q": "When an emergency vehicle approaches with siren and flashing lights, you should:",
        "options": [
            "Speed up to get out of the way",
            "Pull over to the right edge of the road and stop until it passes",
            "Stop immediately in your lane",
            "Continue at the same speed",
        ],
        "answer": 1,
        "explain": "Pull to the right edge and stop, clearing intersections. Do not stop in the middle of an intersection.",
    },
    {
        "q": "In fog, the best headlight setting to use is:",
        "options": [
            "High beams",
            "Low beams",
            "Parking lights only",
            "Hazard lights while driving",
        ],
        "answer": 1,
        "explain": "High beams reflect off fog and reduce visibility. Use low beams (or fog lights) and slow down in fog.",
    },
    {
        "q": "A road is usually most slippery:",
        "options": [
            "After it has been raining for an hour",
            "During the first few minutes of rain, before oil washes away",
            "Only when it is snowing",
            "Only in freezing temperatures",
        ],
        "answer": 1,
        "explain": "The first rain lifts oil and dust into a slick film. Roads are most slippery right after rain begins, before it washes away.",
    },
    {
        "q": "If you become very drowsy while driving, the safest thing to do is:",
        "options": [
            "Turn up the radio and open a window",
            "Pull over somewhere safe and rest",
            "Drink coffee and keep going",
            "Drive faster to arrive sooner",
        ],
        "answer": 1,
        "explain": "Tricks like fresh air or loud music don't cure fatigue. The only real fix is to stop somewhere safe and rest.",
    },
    {
        "q": "The 'No-Zone' around a large truck refers to:",
        "options": [
            "The area where trucks may not drive",
            "The truck's large blind spots where your car disappears from the driver's view",
            "A no-parking zone near loading docks",
            "The space a truck needs to turn right",
        ],
        "answer": 1,
        "explain": "Large trucks have big blind spots (front, rear, and both sides). If you can't see the driver's mirrors, they can't see you — avoid lingering there.",
    },
    {
        "q": "When you want to pass a bicyclist on the road, you should:",
        "options": [
            "Sound your horn and pass close by",
            "Slow down and pass at a safe distance, giving them room",
            "Expect the cyclist to ride onto the shoulder",
            "Pass only on a curve",
        ],
        "answer": 1,
        "explain": "Treat bicyclists as legal road users. Slow down and leave plenty of space (NC law directs at least several feet) when passing.",
    },
    {
        "q": "In North Carolina, the minimum age to get a Level 1 limited learner permit is:",
        "options": ["14", "15", "16", "18"],
        "answer": 1,
        "explain": "You can apply for a Level 1 limited learner permit at age 15 (with driver-education completion), then must hold it at least 12 months.",
    },
    {
        "q": "A Level 2 (limited provisional) driver in NC may carry:",
        "options": [
            "Any number of passengers",
            "No more than one passenger under 21 who isn't an immediate family member",
            "Only adult passengers",
            "No passengers at all",
        ],
        "answer": 1,
        "explain": "Level 2 limits you to at most one passenger under age 21 (immediate family living in your home don't count) to reduce distractions.",
    },
    {
        "q": "Texting on a cell phone while driving in North Carolina is:",
        "options": [
            "Legal for adults",
            "Illegal for all drivers",
            "Legal at stop lights",
            "Illegal only for drivers under 18",
        ],
        "answer": 1,
        "explain": "Reading or sending text messages while driving is illegal for every NC driver, regardless of age.",
    },
    {
        "q": "You must yield the right of way to a funeral procession:",
        "options": [
            "Only if a police officer is present",
            "Yes — do not cut into or interrupt a procession",
            "No, they must yield to normal traffic",
            "Only at red lights",
        ],
        "answer": 1,
        "explain": "Vehicles in a funeral procession have the right of way. Don't drive between the vehicles or interrupt the procession.",
    },
    {
        "q": "You may make a left turn on a red light:",
        "options": [
            "Never",
            "From a one-way street onto another one-way street, after a full stop, unless posted otherwise",
            "Onto any street after stopping",
            "Only with a green arrow",
        ],
        "answer": 1,
        "explain": "A left on red is allowed from a one-way street onto another one-way street, after stopping and yielding, unless a sign prohibits it.",
    },
    {
        "q": "When backing your vehicle, you should:",
        "options": [
            "Rely only on your mirrors",
            "Turn and look through the rear window, and back slowly",
            "Back up as quickly as possible",
            "Sound your horn continuously",
        ],
        "answer": 1,
        "explain": "Mirrors have blind spots. Turn your head to look directly behind you (and use a backup camera if equipped), and back up slowly.",
    },
    {
        "q": "The total distance to stop your vehicle is made up of:",
        "options": [
            "Braking distance only",
            "Perception distance, reaction distance, and braking distance",
            "Following distance and speed",
            "Reaction time only",
        ],
        "answer": 1,
        "explain": "Stopping distance = how far you travel while noticing the hazard (perception) + while moving your foot to the brake (reaction) + while the brakes stop the car (braking). All three grow with speed.",
    },
    {
        "q": "Driving faster than you can stop within the distance lit by your headlights is called:",
        "options": [
            "Overdriving your headlights",
            "Hydroplaning",
            "Coasting",
            "Tailgating",
        ],
        "answer": 0,
        "explain": "At night, don't 'overdrive your headlights' — keep your speed low enough to stop within the distance you can actually see ahead.",
    },
    {
        "q": "A flashing yellow arrow at an intersection means:",
        "options": [
            "Turn is protected — go",
            "You may turn in that direction but must first yield to oncoming traffic and pedestrians",
            "Stop and wait",
            "The signal is broken",
        ],
        "answer": 1,
        "explain": "A flashing yellow arrow means you may turn after yielding to oncoming traffic and pedestrians — the turn is permitted but not protected.",
    },
    {
        "q": "If your vehicle's right wheels drift onto the road shoulder, you should:",
        "options": [
            "Brake hard and jerk the wheel back onto the road",
            "Ease off the gas, hold the wheel firmly, and steer gently back when it's safe",
            "Accelerate to return to the lane quickly",
            "Come to a stop on the shoulder immediately",
        ],
        "answer": 1,
        "explain": "Don't overcorrect. Ease off the accelerator, keep a firm grip, and steer smoothly back onto the pavement when the way is clear.",
    },
    {
        "q": "You are approaching a green light that has been green for a long time. You should:",
        "options": [
            "Speed up to beat the light",
            "Be prepared to stop in case it changes to yellow",
            "Assume it will stay green",
            "Stop and wait for it to cycle",
        ],
        "answer": 1,
        "explain": "A 'stale' green may change at any moment. Cover the brake and be ready to stop safely rather than racing the light.",
    },
    {
        "q": "It is illegal to pass another vehicle by driving:",
        "options": [
            "In the left lane of a multi-lane one-way road",
            "On the shoulder or off the paved roadway",
            "On a road with a dashed yellow line on your side",
            "Anywhere over 45 mph",
        ],
        "answer": 1,
        "explain": "You may not pass by driving off the paved road or on the shoulder. Passing on the right is allowed only within the roadway when there's a usable lane.",
    },
    {
        "q": "Roadway signs that give you directions and distances to towns are colored:",
        "options": ["Red", "Green", "Yellow", "Orange"],
        "answer": 1,
        "explain": "Green guide signs give directions, distances, and route information. Blue signs mark motorist services; brown marks parks and recreation.",
    },
    {
        "q": "A steady yellow traffic light means:",
        "options": [
            "Speed up to clear the intersection",
            "The light is about to turn red — stop if you can do so safely",
            "Stop and stay stopped",
            "Proceed with caution, the same as flashing yellow",
        ],
        "answer": 1,
        "explain": "A steady yellow warns that red is next. Stop if you can do so safely; enter the intersection only if stopping would be unsafe.",
    },
    {
        "q": "If you are being tailgated by another driver, the safest response is to:",
        "options": [
            "Brake suddenly to warn them",
            "Speed up to get away",
            "Move over or slow gradually and let them pass",
            "Ignore them and keep your speed",
        ],
        "answer": 2,
        "explain": "Don't brake-check a tailgater. Increase your own following distance and let them get around you — change lanes or ease off so they can pass.",
    },
    {
        "q": "Financial responsibility (liability insurance) in North Carolina is:",
        "options": [
            "Optional for experienced drivers",
            "Required to register and keep license plates on a vehicle",
            "Only required for commercial vehicles",
            "Required only after an accident",
        ],
        "answer": 1,
        "explain": "NC requires proof of liability insurance to register a vehicle and keep plates. Driving without it can cost your registration and license.",
    },
    {
        "q": "When you see a pedestrian using a white cane or guide dog, you must:",
        "options": [
            "Honk to alert them",
            "Yield the right of way and stop if necessary",
            "Proceed — they will wait for you",
            "Speed up to pass before they cross",
        ],
        "answer": 1,
        "explain": "A white cane or guide dog signals a pedestrian who is blind or visually impaired. State law requires you to yield and stop to let them cross safely.",
    },
    {
        "q": "The center lane marked on both sides with a solid and a dashed yellow line is:",
        "options": [
            "A passing lane",
            "A two-way left-turn lane, shared by both directions for left turns only",
            "A carpool lane",
            "A bicycle lane",
        ],
        "answer": 1,
        "explain": "This center lane is a shared two-way left-turn lane. Use it only to make (or wait to make) a left turn — never to pass or travel through.",
    },
]

_STATES = {
    "nc": {
        "name": "North Carolina",
        "meta": {
            # Real NC knowledge test: 25 questions, must answer 20 correctly (80%) to pass.
            "official_test_size": 25,
            "pass_percent": 80,
            "handbook": "NCDMV Driver's Handbook",
        },
        "questions": _NC_QUESTIONS,
    },
}


def get_states() -> list[dict]:
    """Available states, for the picker — code + display name + question count."""
    return [
        {"code": code, "name": s["name"], "count": len(s["questions"])}
        for code, s in _STATES.items()
    ]


def get_bank(state: str) -> dict | None:
    """The full, UNshuffled question bank for one state, each question carrying a stable
    integer `id` (its position in the master list). The chat-driven quiz uses these ids
    to draw questions without repeats and to remember progress across page reloads — so
    the shuffling and no-repeat bookkeeping live client-side, not here. Returns None for
    an unknown state."""
    state = (state or "").lower()
    data = _STATES.get(state)
    if not data:
        return None
    questions = [
        {"id": i, "q": q["q"], "options": q["options"], "answer": q["answer"], "explain": q["explain"]}
        for i, q in enumerate(data["questions"])
    ]
    return {
        "state": state,
        "name": data["name"],
        "meta": data["meta"],
        "questions": questions,
    }


def get_quiz(state: str, count: int | None = None) -> dict | None:
    """A shuffled quiz for one state: questions in random order (so retakes differ),
    capped at `count` if given. Returns None for an unknown state so the route can 404
    cleanly. Includes each question's answer and explanation — this is a practice aid
    that gives instant feedback, not a proctored exam, so answers living client-side is
    fine and keeps the quiz snappy (no per-answer round trip)."""
    state = (state or "").lower()
    data = _STATES.get(state)
    if not data:
        return None
    questions = list(data["questions"])
    random.shuffle(questions)
    if count:
        questions = questions[:count]
    return {
        "state": state,
        "name": data["name"],
        "meta": data["meta"],
        "questions": questions,
    }
