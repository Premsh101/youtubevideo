"""Public-domain classics behind the most-viewed toddler videos on YouTube.

Lyrics here are traditional/public-domain (we deliberately leave out copyrighted hits
such as Pinkfong's "Baby Shark" or film songs like "Lakdi Ki Kathi").  Gemini turns the
chosen text into the EN+HI scene script exactly as with a user-pasted poem.
"""

PRESETS = [
    {"id": "johny-johny", "title": "Johny Johny Yes Papa", "lang": "en",
     "poem": "Johny Johny, yes papa?\nEating sugar? No papa.\nTelling lies? No papa.\nOpen your mouth! Ha ha ha!"},
    {"id": "wheels-on-the-bus", "title": "Wheels on the Bus", "lang": "en",
     "poem": "The wheels on the bus go round and round, round and round, round and round.\n"
             "The wheels on the bus go round and round, all through the town.\n"
             "The wipers on the bus go swish swish swish...\nThe horn on the bus goes beep beep beep...\n"
             "The babies on the bus go wah wah wah...\nThe mummies on the bus go shh shh shh..."},
    {"id": "twinkle", "title": "Twinkle Twinkle Little Star", "lang": "en",
     "poem": "Twinkle, twinkle, little star, how I wonder what you are.\nUp above the world so high, like a diamond in the sky.\n"
             "Twinkle, twinkle, little star, how I wonder what you are."},
    {"id": "five-little-ducks", "title": "Five Little Ducks", "lang": "en",
     "poem": "Five little ducks went out one day, over the hills and far away.\nMother duck said quack quack quack quack,\n"
             "but only four little ducks came back.\n(Four... three... two... one...)\nSad mother duck went out one day...\n"
             "and all of the five little ducks came back!"},
    {"id": "bath-time", "title": "Bath Song (original)", "lang": "en",
     "poem": None, "topic": "a happy bath time: splashing bubbles, washing hair, rubber duck, getting clean and cozy"},
    {"id": "finger-family", "title": "Finger Family", "lang": "en",
     "poem": "Daddy finger, daddy finger, where are you?\nHere I am, here I am, how do you do?\n"
             "Mummy finger... Brother finger... Sister finger... Baby finger, where are you?\nHere I am, here I am, how do you do?"},
    {"id": "machli", "title": "Machli Jal Ki Rani Hai", "lang": "hi",
     "poem": "मछली जल की रानी है,\nजीवन उसका पानी है।\nहाथ लगाओ डर जाएगी,\nबाहर निकालो मर जाएगी।"},
    {"id": "chanda-mama", "title": "Chanda Mama Door Ke", "lang": "hi",
     "poem": "चंदा मामा दूर के,\nपुए पकाएँ बूर के।\nआप खाएँ थाली में,\nमुन्ने को दें प्याली में।"},
    {"id": "aloo-kachaloo", "title": "Aloo Kachaloo", "lang": "hi",
     "poem": "आलू कचालू बेटा कहाँ गए थे?\nबंदर की झोपड़ी में सो रहे थे।\nबंदर ने लात मारी रो रहे थे,\nमम्मी ने प्यार किया हँस रहे थे।"},
    {"id": "ek-mota-hathi", "title": "Ek Mota Hathi", "lang": "hi",
     "poem": "एक मोटा हाथी झूम के चला,\nमकड़ी के जाले में जा के फँसा।\nएक मोटा हाथी, दो मोटे हाथी,\nतीन मोटे हाथी झूम के चले।"},
    {"id": "titli-udi", "title": "Titli Udi", "lang": "hi",
     "poem": "तितली उड़ी, बस पे चढ़ी,\nसीट न मिली तो रोने लगी।\nड्राइवर बोला आजा मेरे पास,\nतितली बोली हट बदमाश।"},
    {"id": "hathi-raja", "title": "Hathi Raja Kahan Chale", "lang": "hi",
     "poem": "हाथी राजा कहाँ चले?\nसूँड हिलाते कहाँ चले?\nमेरे घर भी आओ ना,\nहलवा पूरी खाओ ना।\nआओ बैठो कुर्सी पर,\nकुर्सी बोली चर चर चर।"},
    {"id": "abc-phonics", "title": "ABC Phonics Song (original)", "lang": "en",
     "poem": None, "topic": "learning letters A to G with a word for each letter (A apple, B ball, C cat...) in a playful phonics song"},
]


def get(pid: str) -> dict | None:
    return next((p for p in PRESETS if p["id"] == pid), None)
