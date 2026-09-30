"""Frameworks as data. Playing to Win is the first; a second framework is a
second entry here (and later, rows in framework/element/test tables — deferred
until one actually exists, per sea-mii7). Element keys match Position.element_key.
"""

FRAMEWORKS = {
    "ptw": {
        "name": "Playing to Win",
        "elements": [
            # key, ordinal, name, required (empty slot is a report finding), prompt
            (
                "ptw.box1",
                1,
                "Winning aspiration",
                True,
                "What does winning look like, and by when?",
            ),
            (
                "ptw.box2",
                2,
                "Where to play",
                True,
                "Which customers, channels, offers — and what is SOLD?",
            ),
            ("ptw.box3", 3, "How to win", True, "Why the buyer moves for us and not inertia."),
            ("ptw.box4", 4, "Capabilities", True, "What must be true of us for 2 and 3 to hold."),
            ("ptw.box5", 5, "Management systems", True, "The instruments that keep 1–4 honest."),
        ],
        "tests": [
            ("t1", "Which of the three roles does it occupy — performance, gift, or residue?"),
            ("t2", "Does it accumulate audience or corpus?"),
            ("t3", "Is it a product being priced, or a capability being given away?"),
            ("t4", "Does it require a castle (defended / exclusive / patented)?"),
            ("t5", "Can it be given away and still generate urgency?"),
            ("t6", "Does it need Ivan physically present to earn?"),
            ("t7", "Does it proliferate — new top-level names vs titles under MCD?"),
        ],
    },
}
