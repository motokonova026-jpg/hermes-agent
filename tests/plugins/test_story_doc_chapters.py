from plugins.story_doc import tools


def test_story_doc_create_content_gets_chapter_one_when_missing():
    content, added = tools._ensure_chapter_heading("The rain began at dusk.", 1)

    assert added is True
    assert content == "Chapter 1\n\nThe rain began at dusk."


def test_story_doc_append_uses_next_chapter_from_existing_doc_text():
    existing = "Chapter 1\n\nOpening scene.\n\nChapter 2\n\nSecond scene."

    assert tools._next_chapter_number(existing) == 3


def test_story_doc_append_does_not_duplicate_existing_chapter_heading():
    content, added = tools._ensure_chapter_heading("Chapter 3\n\nThe next scene.", 3)

    assert added is False
    assert content == "Chapter 3\n\nThe next scene."
