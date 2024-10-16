import uvicorn # For debugging purposes
import hashlib
from fastapi import FastAPI
from yt_dlp.YoutubeDL import YoutubeDL, DownloadError
from urllib.parse import urlparse, parse_qs, ParseResult
from googleapiclient.discovery import build
from dotenv import load_dotenv
import re, os
from datetime import datetime
import pytz

load_dotenv()
api_key = os.getenv("apikey")

# Define the options to use specific extractors
ydl_opts = {
    "allowed_extractors": ["twitter", "Newgrounds", "lbry", "TikTok", "PeerTube", "vimeo", "BiliBili", "dailymotion", "generic"]
}

yt = build("youtube", "v3", developerKey=api_key)

yt_cache = {}

def extract_video_id(url_components):
    """Given a YouTube video URL, extract the video id from it, or None if
    no video id can be extracted."""
    video_id = None

    path = url_components.path
    query_params = parse_qs(url_components.query)

    # Regular YouTube URL: eg. https://www.youtube.com/watch?v=9RT4lfvVFhA
    if path == "/watch":
        video_id = query_params["v"][0]
    else:
        livestream_match = re.match("^/live/([a-zA-Z0-9_-]+)", path)
        shortened_match = re.match("^/([a-zA-Z0-9_-]+)", path)

        if livestream_match:
            # Livestream URL: eg. https://www.youtube.com/live/Q8k4UTf8jiI
            video_id = livestream_match.group(1)
        elif shortened_match:
            # Shortened YouTube URL: eg. https://youtu.be/9RT4lfvVFhA
            video_id = shortened_match.group(1)

    return video_id

def convert_iso8601_duration_to_seconds(iso8601_duration: str) -> int:
    """Given an ISO 8601 duration string, return the length of that duration in
    seconds.

    Note: Apparently the isodate package can perform this conversion if needed.
    """
    if iso8601_duration.startswith("PT"):
        iso8601_duration = iso8601_duration[2:]

    total_seconds, hours, minutes, seconds = 0, 0, 0, 0

    if "H" in iso8601_duration:
        hours_part, iso8601_duration = iso8601_duration.split("H")
        hours = int(hours_part)

    if "M" in iso8601_duration:
        minutes_part, iso8601_duration = iso8601_duration.split("M")
        minutes = int(minutes_part)

    if "S" in iso8601_duration:
        seconds_part = iso8601_duration.replace("S", "")
        seconds = int(seconds_part)

    total_seconds = hours * 3600 + minutes * 60 + seconds

    return total_seconds

def fetch_youtube(url_components):
    video_id = extract_video_id(url_components)

    if not video_id:
        return {"Invalid": "No video id present"}
    
    video_data = yt_cache.get(video_id)

    if video_data:
        return video_data

    request = yt.videos().list(
        part="status,snippet,contentDetails", id=video_id
    )
    response = request.execute()

    if not response["items"]:
        return {"Invalid": "Url doesn't point to a video"}

    response_item = response["items"][0]
    snippet = response_item["snippet"]
    iso8601_duration = response_item["contentDetails"]["duration"]

    video_data = {
        "title": snippet["title"],
        "uploader": snippet["channelTitle"],
        "upload_date": int(datetime.fromisoformat(snippet["publishedAt"]).timestamp()),
        "duration": convert_iso8601_duration_to_seconds(iso8601_duration)
    }

    yt_cache[video_id] = video_data
    return video_data

accepted_domains = [
    "dailymotion.com",
    "pony.tube",
    "vimeo.com",
    "bilibili.com",
    "thishorsie.rocks",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "odysee.com",
    "newgrounds.com"
]

ytdlp_cache = {domain: {} for domain in accepted_domains}

def fetch_ytdlp(url_components: ParseResult):
    netloc = url_components.netloc
    
    if netloc.find(".") != netloc.rfind("."):
        netloc = netloc.split(".", 1)[1]

    if netloc not in accepted_domains:
        return {"Invalid": "Url not from an accepted domain"}
    
    video_id = url_components.path.split("?")[0].rstrip("/").split("/")[-1]
    video_data = ytdlp_cache[netloc].get(video_id)

    if video_data:
        return video_data

    url = url_components.geturl()
    preprocess_changes = preprocess(url_components)

    if preprocess_changes and preprocess_changes.get("url"):
        url = preprocess_changes.pop("url")

    with YoutubeDL(ydl_opts) as ydl:
        try:
            response = ydl.extract_info(url, download=False)
        except DownloadError:
            return {"Invalid": "Url doesn't point to a video"}
        
        if "entries" in response:
            response = response["entries"][0]

        # preprocess_changes contains the response key that should be assigned a new value,
        # and corrected, which can either be a different response key that has the value we
        # originally wanted, None if the response key has an incorrect value with no substitutes,
        # or a lambda function that modifies the value assigned to the respose key
        if len(preprocess_changes):
            for response_key, corrected in preprocess_changes.items():
                if corrected is None:
                    response[response_key] = None
                elif isinstance(corrected, str):
                    response[response_key] = response.get(corrected)
                else:
                    response[response_key] = corrected(response)
        
        upload_date = datetime.strptime(response.get("upload_date"), "%Y%m%d")
        upload_date = pytz.utc.localize(upload_date)

        video_data = {
            "title": response.get("title"),
            "uploader": response.get("channel"),
            "upload_date": upload_date.timestamp(),
            "duration": response.get("duration"),
        }

        ytdlp_cache[response["webpage_url_domain"]][response["display_id"]] = video_data
        return video_data

# Some urls might have specific issues that should
# be handled here before they can be properly processed
# If yt-dlp gets any updates that resolve any of these issues
# then the respective case should be updated accordingly
def preprocess(url_components: ParseResult) -> dict:
    site = url_components.netloc.split(".")
    site = site[0] if len(site) == 2 else site[1]
    
    changes = {}

    match site:
        case "x":
            new_url = "https://twitter.com" + url_components.path
            changes = preprocess(urlparse(new_url))
            changes["url"] = new_url

        case "twitter":
            changes["channel"] = "uploader_id"
            changes["title"] = (
                lambda vid_data: f"X post by {vid_data.get('uploader_id')} ({hash_str(vid_data.get('title'))})"
            )

            # This type of url means that the post has more than one video
            # and ytdlp will only successfully retrieve the duration if
            # the video is at index one
            url = url_components.geturl()
            if (
                url[0 : url.rfind("/")].endswith("/video")
                and int(url[url.rfind("/") + 1 :]) != 1
            ):
                changes["duration"] = None

        case "newgrounds":
            changes["channel"] = "uploader"

        case "tiktok":
            changes["channel"] = "uploader"
            changes["title"] = (
                lambda vid_data: f"Tiktok video by {vid_data.get('uploader')} ({hash_str(vid_data.get('title'))})"
            )

        case "bilibili":
            changes["channel"] = "uploader"

    return changes

# Some sites like X and Tiktok don't have a designated place to put a title for
# posts so the 'titles' are hashed here to reduce the chance of similarity detection
# between different posts by the same uploader. Larger hash substrings decrease this chance
def hash_str(string):
    h = hashlib.sha256()
    h.update(string.encode())
    return h.hexdigest()[:5]


youtube_domains = ["m.youtube.com", "www.youtube.com", "youtube.com", "youtu.be"]

app = FastAPI()

@app.post("/fetch")
def update_item(urls: list[str]):
    urls: list[ParseResult] = [urlparse(url) for url in urls]
    return [fetch_youtube(url_components) if url_components.netloc in youtube_domains else fetch_ytdlp(url_components) for url_components in urls]


if __name__ == "__main__":
    uvicorn.run(app)

# "[\"https://www.newgrounds.com/portal/view/759280\", \"https://twitter.com/doubleWbrothers/status/1786396472105115712\", \"https://odysee.com/@DeletedBronyVideosArchive:d/blind-reaction-review-mlp-make-your-3:0\", \"https://www.tiktok.com/@kyukenn__/video/7338022224466562309?q=my%20little%20pony\"]"