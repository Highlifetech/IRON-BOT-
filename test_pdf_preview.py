import json
import fitz
import pdf_preview


def _pdf(pages):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"SO6926 page {i+1}")
    return doc.tobytes()


class FakeLark:
    def __init__(self, pdf):
        self.pdf, self.uploads, self.posts = pdf, [], []
    def get_message_resource(self, mid, key, kind):
        assert kind == "file"
        return self.pdf
    def upload_image(self, b):
        assert b[:4] == b"\x89PNG"
        self.uploads.append(b)
        return f"img_{len(self.uploads)}"
    def _post(self, path, body=None):
        self.posts.append((path, body))
        return {"code": 0}


def _msg(name="SO6926.pdf", mtype="file"):
    return {"message_type": mtype, "message_id": "om_1", "chat_id": "oc_po",
            "content": json.dumps({"file_key": "file_x", "file_name": name})}


def test_filters():
    assert pdf_preview.should_preview(_msg(), "ou_hannah", "ou_bot")
    assert not pdf_preview.should_preview(_msg("art.ai"), "ou_hannah", "ou_bot")
    assert not pdf_preview.should_preview(_msg(), "ou_bot", "ou_bot")
    assert not pdf_preview.should_preview(_msg(mtype="text"), "ou_hannah", "ou_bot")


def test_reply_card_caps_pages():
    lark = FakeLark(_pdf(5))
    pdf_preview.handle_pdf_message(lark, _msg())
    assert len(lark.uploads) == pdf_preview.MAX_PAGES == 3
    path, body = lark.posts[0]
    assert path == "/open-apis/im/v1/messages/om_1/reply"
    card = json.loads(body["content"])
    assert [e["img_key"] for e in card["elements"] if e["tag"] == "img"] == ["img_1", "img_2", "img_3"]
    assert "of 5" in card["elements"][-1]["elements"][0]["content"]


def test_single_page():
    lark = FakeLark(_pdf(1))
    pdf_preview.handle_pdf_message(lark, _msg())
    card = json.loads(lark.posts[0][1]["content"])
    assert card["elements"][-1]["elements"][0]["content"] == "Page 1 of 1"


def test_bad_pdf_does_not_raise():
    lark = FakeLark(b"not a pdf")
    pdf_preview.handle_pdf_message(lark, _msg())
    assert lark.posts == []
