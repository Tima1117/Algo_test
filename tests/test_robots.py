from pif.robots import Robots

TXT = """
User-agent: Yandex
Allow: /

User-agent: *
Disallow: /*?
Disallow: /*.pdf$
Disallow: /api/
Allow: /api/public/
"""


def test_wildcards_and_longest_match():
    r = Robots(TXT, "pif-registry/0.1")
    assert r.allowed("https://x.ru/funds/abc/")
    assert not r.allowed("https://x.ru/funds/?page=2")
    assert not r.allowed("https://x.ru/files/kid.pdf")
    assert r.allowed("https://x.ru/files/kid.pdf.html")
    assert not r.allowed("https://x.ru/api/nav")
    assert r.allowed("https://x.ru/api/public/nav")


def test_disallow_all_and_empty():
    assert not Robots("User-agent: *\nDisallow: /", "pif-registry").allowed("https://x.ru/api/v1/x")
    assert Robots("User-agent: *\nDisallow:", "pif-registry").allowed("https://x.ru/a")
    assert Robots("", "pif-registry").allowed("https://x.ru/a")
