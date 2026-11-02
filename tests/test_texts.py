from bot import texts


def test_topics():
    assert len(texts.TOPICS) == 30
    assert texts.TOPICS[0] == "Почему без запретов работает лучше диет"
    assert texts.TOPICS[-1] == "Как держать вес после цели"
    assert len(set(texts.TOPICS)) == 30
