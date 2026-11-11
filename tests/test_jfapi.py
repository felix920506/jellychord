import unittest
import urllib.parse

from jfapi import JFAPI


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get(self, endpoint, params):
        self.requests.append((endpoint, params))
        return FakeResponse(self.payload)


class JFAPITests(unittest.IsolatedAsyncioTestCase):
    def test_get_audio_hls_builds_expected_stream_url(self):
        api = JFAPI("https://jellyfin.example.com/base/", "secret-key")

        url = api.getAudioHls("item-1", 128000)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(
            parsed.scheme + "://" + parsed.netloc + parsed.path,
            "https://jellyfin.example.com/base/Audio/item-1/main.m3u8",
        )
        self.assertEqual(query["ApiKey"], ["secret-key"])
        self.assertEqual(query["segmentContainer"], ["mp4"])
        self.assertEqual(query["audioCodec"], ["opus"])
        self.assertEqual(query["allowAudioStreamCopy"], ["True"])
        self.assertEqual(query["maxAudioBitDepth"], ["16"])
        self.assertEqual(query["audioSampleRate"], ["48000"])
        self.assertEqual(query["audioChannels"], ["2"])
        self.assertEqual(query["audioBitRate"], ["128000"])

    async def test_search_returns_items_and_sends_filters(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "track-1"}]})
        api._session = session

        result = await api.search("mint", limit=5, types=["Audio", "MusicAlbum"])

        self.assertEqual(result, [{"Id": "track-1"}])
        self.assertEqual(len(session.requests), 1)
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(
            params,
            {
                "ApiKey": "secret-key",
                "searchTerm": "mint",
                "recursive": "true",
                "limit": 5,
                "includeItemTypes": "Audio,MusicAlbum",
            },
        )

    async def test_get_items_by_ids_joins_ids_for_query(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "a"}, {"Id": "b"}]})
        api._session = session

        result = await api.getItemsByIds(["a", "b"])

        self.assertEqual(result, [{"Id": "a"}, {"Id": "b"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(params, {"ApiKey": "secret-key", "ids": "a,b"})

    async def test_get_album_tracks_requests_parent_sorted_by_disc_and_index(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "track-1"}]})
        api._session = session

        result = await api.getAlbumTracks("album-1")

        self.assertEqual(result, [{"Id": "track-1"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(
            params,
            {
                "ApiKey": "secret-key",
                "parentId": "album-1",
                "sortBy": "ParentIndexNumber,IndexNumber",
            },
        )

    async def test_get_random_items_requests_random_sort_within_library(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "track-1"}]})
        api._session = session

        result = await api.getRandomItems("Audio", "library-1")

        self.assertEqual(result, [{"Id": "track-1"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(
            params,
            {
                "ApiKey": "secret-key",
                "includeItemTypes": "Audio",
                "recursive": "true",
                "sortBy": "Random",
                "limit": 1,
                "parentId": "library-1",
            },
        )

    async def test_get_random_items_omits_parent_when_no_library_given(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": []})
        api._session = session

        result = await api.getRandomItems("MusicAlbum")

        self.assertEqual(result, [])
        _, params = session.requests[0]
        self.assertEqual(params["includeItemTypes"], "MusicAlbum")
        self.assertNotIn("parentId", params)

    async def test_get_music_libraries_keeps_only_music_collections(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession(
            {
                "Items": [
                    {"Id": "lib-music", "Name": "Music", "CollectionType": "music"},
                    {"Id": "lib-movies", "Name": "Movies", "CollectionType": "movies"},
                    {"Id": "lib-mixed", "Name": "Mixed"},
                ]
            }
        )
        api._session = session

        result = await api.getMusicLibraries()

        self.assertEqual(result, [{"Id": "lib-music", "Name": "Music", "CollectionType": "music"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Library/MediaFolders")
        self.assertEqual(params, {"ApiKey": "secret-key"})


if __name__ == "__main__":
    unittest.main()
