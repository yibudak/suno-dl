from __future__ import annotations

from suno_dl import (
    clip_artist,
    clip_audio_urls,
    clip_cover_url,
    clip_duration,
    clip_title,
    usable_url,
)

UUID = "11111111-2222-4333-8444-555555555555"


def test_title_and_artist_defaults():
    assert clip_title({}) == "untitled"
    assert clip_artist({}) == "Unknown Artist"
    assert clip_artist({"handle": "someone"}) == "someone"
    assert clip_artist({"display_name": "Some One", "handle": "someone"}) == "Some One"
    assert clip_artist({"user": {"handle": "nested"}}) == "nested"


def test_duration_prefers_metadata():
    assert clip_duration({"metadata": {"duration": 226.8}}) == 226.8
    assert clip_duration({"duration": 12}) == 12
    assert clip_duration({"metadata": {"duration": "x"}}) == 0.0
    assert clip_duration({}) == 0.0


def test_forbidden_urls_are_dropped():
    assert usable_url("https://studio-api.prod.suno.com/api/forbidden") is None
    assert usable_url("http://insecure.example/a.mp4") is None
    assert usable_url(None) is None
    assert usable_url("https://cdn1.suno.ai/a.mp4") == "https://cdn1.suno.ai/a.mp4"


def test_audio_urls_prefer_the_cdn_video_file():
    clip = {
        "id": UUID,
        "video_url": f"https://cdn1.suno.ai/{UUID}.mp4",
        "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
        "media_urls": [{"url": "https://example.invalid/a.m4a"}],
    }
    urls = clip_audio_urls(clip)
    assert urls[0] == f"https://cdn1.suno.ai/{UUID}.mp4"
    assert urls[1] == "https://example.invalid/a.m4a"
    assert all("forbidden" not in url for url in urls)


def test_audio_urls_fall_back_to_the_cdn_pattern():
    urls = clip_audio_urls(
        {"id": UUID, "audio_url": "https://studio-api.prod.suno.com/api/forbidden"}
    )
    assert urls == [f"https://cdn1.suno.ai/{UUID}.mp4"]


def test_audio_urls_are_deduplicated():
    same = f"https://cdn1.suno.ai/{UUID}.mp4"
    assert clip_audio_urls({"id": UUID, "video_url": same}) == [same]


def test_cover_url_prefers_the_large_image():
    clip = {
        "image_url": "https://cdn2.suno.ai/small.jpeg",
        "image_large_url": "https://cdn2.suno.ai/large.jpeg",
    }
    assert clip_cover_url(clip) == "https://cdn2.suno.ai/large.jpeg"
    assert clip_cover_url({"image_url": "https://cdn2.suno.ai/small.jpeg"}).endswith("small.jpeg")
    assert clip_cover_url({}) is None
