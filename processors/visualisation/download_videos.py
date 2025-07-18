"""
Download videos

First attempt to download via request, but if that fails use yt-dlp
"""
import os
import json
import re
import time
import zipfile
from pathlib import Path
import requests
import yt_dlp
from ural import urls_from_text
from urllib.parse import urlparse
from yt_dlp import DownloadError
from yt_dlp.utils import ExistingVideoReached

from backend.lib.processor import BasicProcessor
from common.lib.dataset import DataSet
from common.lib.exceptions import ProcessorInterruptedException, ProcessorException, DataSetException
from common.lib.helpers import UserInput, sets_to_lists, url_to_filename

__author__ = "Dale Wahl"
__credits__ = ["Dale Wahl"]
__maintainer__ = "Dale Wahl"
__email__ = "4cat@oilab.eu"


class VideoDownloaderPlus(BasicProcessor):
    """
    Downloads videos and saves as zip archive

    Attempts to download videos directly, but if that fails, uses YT_DLP. (https://github.com/yt-dlp/yt-dlp/#readme)
    which attempts to keep up with a plethora of sites: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
    """
    type = "video-downloader"  # job type ID
    category = "Visual"  # category
    title = "Download videos"  # title displayed in UI
    description = "Download videos from URLs and store in a zip file. May take a while to complete as videos are " \
                  "retrieved externally."  # description displayed in UI
    extension = "zip"  # extension of result file, used internally and in UI
    media_type = "video"  # media type of the processor

    followups = ["audio-extractor", "metadata-viewer", "video-scene-detector", "preset-scene-timelines", "video-stack", "preset-video-hashes", "video-hasher-1", "video-frames"]

    references = [
        "[YT-DLP python package](https://github.com/yt-dlp/yt-dlp/#readme)",
        "[Supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)",
    ]

    known_channels = ['youtube.com/c/', 'youtube.com/channel/']

    options = {
        "amount": {
            "type": UserInput.OPTION_TEXT,
            "help": "No. of videos (max 1000)",
            "default": 100,
            "min": 0,
        },
        "columns": {
            "type": UserInput.OPTION_TEXT,
            "help": "Column to get video links from",
            "inline": True,
            "tooltip": "If the column contains a single URL, use that URL; else, try to find image URLs in the "
                       "column's content"
        },
        "max_video_size": {
            "type": UserInput.OPTION_TEXT,
            "help": "Max videos size (in MB/Megabytes)",
            "default": 100,
            "min": 1,
            "tooltip": "Max of 100 MB set by 4CAT administrators",
        },
        "split-comma": {
            "type": UserInput.OPTION_TOGGLE,
            "help": "Split column values by comma",
            "default": True,
            "tooltip": "If enabled, columns can contain multiple URLs separated by commas, which will be considered "
                       "separately"
        },
    }

    config = {
        "video-downloader.ffmpeg_path": {
            "type": UserInput.OPTION_TEXT,
            "default": "ffmpeg",
            "help": "Path to ffmpeg",
            "tooltip": "Where to find the ffmpeg executable. ffmpeg is required by many of the video-related "
                       "processors which will be unavailable if no executable is available in this path."
        },
        "video-downloader.max": {
            "type": UserInput.OPTION_TEXT,
            "coerce_type": int,
            "default": 1000,
            "help": "Max number of videos to download",
            "tooltip": "Only allow downloading up to this many videos per batch. Increasing this can lead to "
                       "long-running processors and large datasets. Set to 0 for no limit."
        },
        "video-downloader.max-size": {
            "type": UserInput.OPTION_TEXT,
            "coerce_type": int,
            "default": 100,
            "help": "Max allowed MB size per video",
            "tooltip": "Size in MB/Megabytes; default 100. 0 will allow any size."
        },
        "video-downloader.allow-unknown-size": {
            "type": UserInput.OPTION_TOGGLE,
            "default": True,
            "help": "Allow video download of unknown size",
            "tooltip": "Video size is not always available before downloading. If True, users may choose to download "
                       "videos with unknown sizes."
        },
        "video-downloader.allow-indirect": {
            "type": UserInput.OPTION_TOGGLE,
            "default": False,
            "help": "Allow indirect downloads",
            "tooltip": "Allow users to choose to download videos linked indirectly (e.g. embedded in a linked tweet, link to a YouTube video). "
                       "Enabling can be confusing for users and download more than intended."
        },
        "video-downloader.allow-multiple": {
            "type": UserInput.OPTION_TOGGLE,
            "default": False,
            "help": "Allow multiple videos per item",
            "tooltip": "Allow users to choose to download videos from links that refer to multiple videos. For "
                       "example, for a given link to a YouTube channel all videos for that channel are downloaded."
        },
    }

    def __init__(self, logger, job, queue=None, manager=None, modules=None):
        super().__init__(logger, job, queue, manager, modules)
        self.max_videos_per_url = 1
        self.videos_downloaded_from_url = None
        self.downloaded_videos = 0
        self.total_possible_videos = 5
        self.url_files = None
        self.last_dl_status = None
        self.last_post_process_status = None

    @classmethod
    def get_options(cls, parent_dataset=None, config=None):
        """
        Updating columns with actual columns and setting max_number_videos per
        the max number of images allowed.
        :param config:
        """
        options = cls.options

        # Update the amount max and help from config
        max_number_videos = int(config.get('video-downloader.max', 100))
        if max_number_videos == 0:
            options['amount']['help'] = "No. of videos"
            options["amount"]["tooltip"] = "Use 0 to download all videos"
        else:
            options['amount']['max'] = max_number_videos
            options['amount']['help'] = f"No. of videos (max {max_number_videos:,})"

        # And update the max size and help from config
        max_video_size = int(config.get('video-downloader.max-size', 100))
        if max_video_size == 0:
            # Allow video of any size
            options["max_video_size"]["tooltip"] = "Set to 0 if all sizes are to be downloaded."
            options['max_video_size']['min'] = 0
        else:
            # Limit video size
            options["max_video_size"]["max"] = max_video_size
            options['max_video_size']['default'] = options['max_video_size']['default'] if options['max_video_size'][
                                                                                       'default'] <= max_video_size else max_video_size
            options["max_video_size"]["tooltip"] = f"Cannot be more than {max_video_size}MB."
            options['max_video_size']['min'] = 1

        # Get the columns for the select columns option
        if parent_dataset and parent_dataset.get_columns():
            columns = parent_dataset.get_columns()
            options["columns"]["type"] = UserInput.OPTION_MULTI
            options["columns"]["options"] = {v: v for v in columns}

            # Figure out default column
            priority = ["video_url", "video_link", "video", "media_url", "media_link", "media", "final_url", "url", "link", "body"]
            columns.sort(key=lambda col: next((i for i, p in enumerate(priority) if p in col.lower()), len(priority)))
            options["columns"]["default"] = [columns.pop(0)]

        # these two options are likely to be unwanted on instances with many
        # users, so they are behind an admin config options
        if config.get("video-downloader.allow-indirect"):
            options["use_yt_dlp"] = {
                "type": UserInput.OPTION_TOGGLE,
                "help": "Also attempt to download non-direct video links (such YouTube and other video hosting sites)",
                "default": False,
                "tooltip": "If False, 4CAT will only download directly linked videos (works with fields like Twitter's \"video\", TikTok's \"video_url\" or Instagram's \"media_url\"), but if True 4CAT uses YT-DLP to download from YouTube and a number of other video hosting sites (see references)."
            }
            options["max_video_res"] = {
                "type": UserInput.OPTION_TEXT,
                "help": "Max video resolution height (use 0 for any)",
                "coerce_type": int,
                "default": 0,
                "min": 0,
                "tooltip": "If 0, any resolution is allowed. Otherwise, only videos with a resolution less than or equal to this height will be downloaded (e.g. videos less than 480p).",
                "requires": "use_yt_dlp=true"
            }

        if config.get("video-downloader.allow-multiple"):
            options["channel_videos"] = {
                                            "type": UserInput.OPTION_TEXT,
                                            "help": "Download multiple videos per link?",
                                            "default": 0,
                                            "min": 0,
                                            "max": 5,
                                            "requires": "use_yt_dlp=true",
                                            "tooltip": "If more than 0, links leading to multiple videos will be downloaded (e.g. a YouTube user's channel)"
                                        }
        if config.get('video-downloader.allow-unknown-size', False):
            options["allow_unknown_size"] = {
                "type": UserInput.OPTION_TOGGLE,
                "help": "Allow unknown video sizes",
                "default": False,
                "tooltip": "If True, videos with unknown sizes will be downloaded (size filters still applies if known). Recommend on using if you are not able to download videos otherwise."
            }

        return options

    @classmethod
    def is_compatible_with(cls, module=None, config=None):
        """
        Determine compatibility

        Compatible with any top-level dataset. Could run on any type of dataset
        in principle, but any links to videos are likely to come from the top
        dataset anyway.

        :param module:  Module to determine compatibility with
        :param ConfigManager|None config:  Configuration reader (context-aware)
        :return bool:
        """
        return ((module.type.endswith("-search") or module.is_from_collector())
                and module.type not in ["tiktok-search", "tiktok-urls-search", "telegram-search"]) \
                and module.get_extension() in ("csv", "ndjson")

    def process(self):
        """
        This takes a 4CAT results file as input, and downloads video files
        referenced therein according to the processor parameters.
        """
        # Check processor able to run
        if self.source_dataset.num_rows == 0:
            self.dataset.update_status("No data from which to extract video URLs.", is_final=True)
            self.dataset.finish(0)
            return

        # Collect URLs
        try:
            urls = self.collect_video_urls()
        except ProcessorException as e:
            self.dataset.update_status(str(e), is_final=True)
            self.dataset.finish(0)
            return

        self.dataset.log('Collected %i urls.' % len(urls))

        vid_lib = DatasetVideoLibrary(self.dataset)

        # Prepare staging area for videos and video tracking
        results_path = self.dataset.get_staging_area()

        # YT-DLP advanced filter
        def dmi_match_filter(vid_info, *, incomplete):
            """
            Another method for ignoring specific videos.
            https://github.com/yt-dlp/yt-dlp#filter-videos
            """
            # Check if video is known to be live (there also exists a `was_live` tag if that's desired)
            if vid_info.get('is_live'):
                raise LiveVideoException("4CAT settings do not allow downloading live videos with this processor")

        # Use YT-DLP
        use_yt_dlp = self.parameters.get("use_yt_dlp", False)

        # Set up YT-DLP options
        ydl_opts = {
            # "logger": self.log,  # This will dump any errors to our logger if desired
            "socket_timeout": 20,
            # TODO: if yt-dlp archive is used, it raises an error, but does not contain the archive info; how to then
            #  connect the URL to the previously downloaded video?! A second request without download_archive and
            #  download=False can get the `info` but need to then use that info to tie to the filename!
            "download_archive": str(results_path.joinpath("video_archive")),
            "break_on_existing": True,
            "postprocessor_hooks": [self.yt_dlp_post_monitor],
            # This function ensures no more than self.max_videos_per_url downloaded and can be used to monitor progress
            "progress_hooks": [self.yt_dlp_monitor],
            'match_filter': dmi_match_filter,
        }

        # Collect parameters
        amount = self.parameters.get("amount", 100)
        if amount == 0:  # unlimited
            amount = self.config.get('video-downloader.max', 100)

        # Set a maximum amount of videos that can be downloaded per URL and set
        # if known channels should be downloaded at all
        self.max_videos_per_url = self.parameters.get("channel_videos", 0)
        if self.max_videos_per_url == 0:
            # TODO: how to ensure unknown channels/playlists are not downloaded? Is it possible with yt-dlp?
            self.max_videos_per_url = 1  # Ensure unknown channels only end up with one video downloaded
            download_channels = False
        else:
            download_channels = True

        # YT-DLP by default attempts to download the best quality videos
        allow_unknown_sizes = self.parameters.get('allow_unknown_size', False)
        max_video_size = self.parameters.get("max_video_size", 100)
        max_size = str(max_video_size) + "M"
        max_video_res = self.parameters.get("max_video_res", 0)
        if max_video_size > 0 or max_video_res > 0:
            filesize_filter = ""
            filesize_approx_filter = ""
            res_filter = ""
            if max_video_size > 0:
                filesize_filter = f"[filesize<={max_size}]"
                filesize_approx_filter = f"[filesize_approx<={max_size}]"
            if max_video_res > 0:
                res_filter = f"[height<={max_video_res}]"

            # Formats may be combined audio/video or separate streams
            if filesize_filter:
                # Use both filesize and filesize_approx to ensure we get the best video available
                ydl_opts["format"] = f"{res_filter}{filesize_filter}/bestvideo{res_filter}{filesize_filter}+bestaudio{filesize_filter}/{res_filter}{filesize_approx_filter}/bestvideo{res_filter}{filesize_approx_filter}+bestaudio{filesize_approx_filter}"
            else:
                ydl_opts["format"] = f"{res_filter}/bestvideo{res_filter}+bestaudio"

            if allow_unknown_sizes:
                # Allow unknown sizes if no video meeting the criteria is found
                ydl_opts["format"] += "/best/bestvideo+bestaudio"

            self.dataset.log(f"YT-DLP format filter: {ydl_opts['format']}")

        # Loop through video URLs and download
        self.downloaded_videos = 0
        failed_downloads = 0
        copied_videos = 0
        consecutive_errors = 0
        not_a_video = 0
        last_domains = []
        self.total_possible_videos = min(len(urls), amount) if amount != 0 else len(urls)
        yt_dlp_archive_map = {}
        for url in urls:
            if self.interrupted:
                raise ProcessorInterruptedException("Interrupted while downloading videos.")

            # Check previously downloaded library
            if url in vid_lib.library:
                previous_vid_metadata = vid_lib.library[url]
                if previous_vid_metadata.get('success', False):
                    # Use previous downloaded video
                    try:
                        self.dataset.log(f"Copying previously downloaded video for url: {url}")
                        num_copied = self.copy_previous_video(previous_vid_metadata, results_path, vid_lib.previous_downloaders)
                        urls[url] = previous_vid_metadata
                        self.dataset.update_status("Copied previously downloaded video to current dataset.")
                        copied_videos += num_copied
                        continue
                    except FailedToCopy as e:
                        # Unable to copy
                        self.dataset.log(f"{str(e)}; attempting to download again")
                elif previous_vid_metadata.get("retry", True) is False:
                    urls[url] = previous_vid_metadata
                    self.dataset.log(f"Skipping; previously identified url as not a video: {url}")
                    continue

            urls[url]["success"] = False
            urls[url]["retry"] = True
            last_domains = last_domains[-4:] + [urlparse(url).netloc]

            # Stop processing if worker has been asked to stop or max downloads reached
            if self.downloaded_videos >= amount and amount != 0:
                urls[url]["error"] = "Max video download limit already reached."
                continue

            # Check for repeated timeouts
            if consecutive_errors >= 5:
                if use_yt_dlp:
                    message = "Downloaded %i videos. Errors %i consecutive times; try " \
                              "deselecting the non-direct videos setting" % (self.downloaded_videos, consecutive_errors)
                else:
                    message = "Downloaded %i videos. Errors %i consecutive times; check logs to ensure " \
                              "video URLs are working links and you are not being blocked." % (self.downloaded_videos, consecutive_errors)
                self.dataset.update_status(message, is_final=True)
                if self.downloaded_videos == 0:
                    self.dataset.finish(0)
                    return
                else:
                    # Finish processor with already downloaded videos
                    break
            if not_a_video >= 10 and last_domains.count(urlparse(url).netloc) == 5:
                # This processor can be used to extract all links from text body and attempt to download any with videos
                # If the same domain is encountered 5 times in a row and no links are to videos, we are assuming the user has poorly chosen their columns and no videos will be found
                self.dataset.update_status(f"Too many consecutive non-video URLs encountered; {'try again with Non-direct videos option selected' if self.config.get('video-downloader.allow-indirect') else 'try extracting URLs and filtering dataset first'}.", is_final=True)
                if self.downloaded_videos == 0:
                    self.dataset.finish(0)
                    return
                else:
                    # Finish processor with already downloaded videos
                    break

            # Reject known channels; unknown will still download!
            if not download_channels and any([sub_url in url for sub_url in self.known_channels]):
                message = 'Skipping known channel: %s' % url
                urls[url]['error'] = message
                failed_downloads += 1
                self.dataset.log(message)
                continue

            self.videos_downloaded_from_url = set()
            # First we'll try to see if we can directly download the URL
            try:
                filename = self.download_video_with_requests(url, results_path, max_video_size)
                urls[url]["downloader"] = "direct_link"
                urls[url]["files"] = [{
                    "filename": filename,
                    "metadata": {},
                    "success": True
                }]
                success = True
                self.videos_downloaded_from_url.add(filename)
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.TooManyRedirects, FilesizeException, FailedDownload, NotAVideo) as e:
                # FilesizeException raised when file size is too large or unknown filesize (and that is disabled in 4CAT settings)
                # FailedDownload raised when response other than 200 received
                # NotAVideo raised due to specific Content-Type known to not be a video (and not a webpage/html that could lead to a video via YT-DLP)
                self.dataset.log(f"Request Error: {str(e)}")
                urls[url]["error"] = str(e)
                if type(e) is requests.exceptions.Timeout:
                    # TODO: retry timeouts?
                    consecutive_errors += 1
                if type(e) in [requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError, FilesizeException, FailedDownload]:
                    failed_downloads += 1
                if type(e) in [NotAVideo]:
                    # No need to retry non videos
                    urls[url]["retry"] = False
                continue
            except VideoStreamUnavailable as e:
                if use_yt_dlp:
                    # Take it away yt-dlp
                    # Update filename
                    ydl_opts["outtmpl"] = str(results_path) + '/' + re.sub(r"[^0-9a-z]+", "_", url.lower())[
                                                                    :100] + '_%(autonumber)s.%(ext)s'
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        # Count and use self.yt_dlp_monitor() to ensure sure we don't download videos forever...
                        self.url_files = {}
                        self.last_dl_status = {}
                        self.last_post_process_status = {}
                        self.dataset.update_status("Downloading %i/%i via yt-dlp: %s" % (self.downloaded_videos + 1, self.total_possible_videos, url))
                        try:
                            ydl.extract_info(url)
                        except MaxVideosDownloaded:
                            self.dataset.log("Max videos for URL reached.")
                            # Raised when already downloaded max number of videos per URL as defined in self.max_videos_per_url
                            pass
                        except ExistingVideoReached:
                            self.dataset.log("Already downloaded video associated with: %s" % url)
                            # Video already downloaded; grab from archive
                            # TODO: with multiple videos per URL, this may not capture the desired video and would instead repeat the first video; Need more feedback from yt-dlp!
                            with yt_dlp.YoutubeDL({"socket_timeout": 30}) as ydl2:
                                info2 = ydl2.extract_info(url, download=False)
                                if info2:
                                    self.url_files[info2.get('_filename', {})] = yt_dlp_archive_map[info2.get('extractor') + info2.get('id')]
                                    self.dataset.log("Already downloaded video associated with: %s" % url)
                                else:
                                    message = f"Video identified, but unable to identify which video from {url}"
                                    self.dataset.log(message)
                                    self.log.warning(message)
                                    if len(self.videos_downloaded_from_url) == 0:
                                        # No videos downloaded for this URL
                                        urls[url]['error'] = message
                                        continue
                        except (DownloadError, LiveVideoException) as e:
                            # LiveVideoException raised when a video is known to be live
                            if "Requested format is not available" in str(e):
                                self.dataset.log(f"Format Error: {str(e)}")
                                message = "No format available for video (check max size/resolution settings and try again)"
                            elif "Unable to download webpage: The read operation timed out" in str(e):
                                # Certain sites fail repeatedly (22-12-8 TikTok has this issue)
                                message = 'DownloadError: %s' % str(e)
                            elif "Sign in to confirm you’re not a bot." in str(e):
                                # Youtube blocks YT-DLP; need to sign in
                                message = 'Sign in required: %s' % str(e)
                            elif "HTTP Error 429: Too Many Requests" in str(e):
                                # Oh no, https://github.com/yt-dlp/yt-dlp/wiki/FAQ#http-error-429-too-many-requests-or-402-payment-required
                                message = 'Too Many Requests: %s' % str(e)
                                # TODO: Add url back to end?
                            else:
                                message = 'DownloadError: %s' % str(e)
                            time.sleep(10 * consecutive_errors)
                            consecutive_errors += 1
                            urls[url]['error'] = message
                            failed_downloads += 1
                            self.dataset.log(message)
                            continue
                        except Exception as e:
                            self.dataset.log(f"YT-DLP raised unexpected error: {str(e)}")
                            # Catch all other issues w/ yt-dlp
                            message = "YT-DLP raised unexpected error: %s" % str(e)
                            urls[url]['error'] = message
                            failed_downloads += 1
                            self.dataset.log(message)

                    # Add file data collected by YT-DLP
                    urls[url]["downloader"] = "yt_dlp"
                    urls[url]['files'] = list(self.url_files.values())
                    # Add to archive mapping in case needed
                    for file in self.url_files.values():
                        yt_dlp_archive_map[
                            file.get('metadata').get('extractor') + file.get('metadata').get('id')] = file

                    # Check that download and processing finished
                    success = all([self.last_dl_status.get('status') == 'finished',
                                   self.last_post_process_status.get('status') == 'finished'])

                else:
                    # No YT-DLP; move on
                    self.dataset.log(f"NotVideoLinkError: {str(e)}")
                    not_a_video += 1
                    urls[url]["error"] = str(e)
                    if last_domains.count(urlparse(url).netloc) >= 2:
                        # Same domain encountered at least twice; let's wait before getting blocked
                        time.sleep(5 * not_a_video)
                    continue

            urls[url]["success"] = success
            if success:
                consecutive_errors = 0
                not_a_video = 0

            # Update status
            self.downloaded_videos += len(self.videos_downloaded_from_url)
            self.dataset.update_status(f"Downloaded {self.downloaded_videos}/{self.total_possible_videos} videos" +
                                       (f"; videos copied from {copied_videos} previous downloads" if copied_videos > 0 else "") +
                                       (f"; {failed_downloads} URLs failed." if failed_downloads > 0 else ""))
            self.dataset.update_progress(self.downloaded_videos / self.total_possible_videos)

        self.dataset.update_status("Updating and saving metadata")
        # Save some metadata to be able to connect the videos to their source
        metadata = {
            url: {
                "from_dataset": self.source_dataset.key,
                **sets_to_lists(data)
                # TODO: This some shenanigans until I can figure out what to do with the info returned
            } for url, data in urls.items()
        }
        with results_path.joinpath(".metadata.json").open("w", encoding="utf-8") as outfile:
            json.dump(metadata, outfile)

        # Finish up
        self.dataset.update_status("Writing downloaded videos to zip archive")
        self.write_archive_and_finish(results_path, self.downloaded_videos+copied_videos)
        self.dataset.update_status(f"Downloaded {self.downloaded_videos} videos" + (
                                       f"; videos copied from {copied_videos} previous downloads" if copied_videos > 0 else "") +
                                   (f"; {failed_downloads} URLs failed." if failed_downloads > 0 else ""),
                                   is_final=True)

    def yt_dlp_monitor(self, d):
        """
        Can be used to gather information from yt-dlp while downloading
        """
        self.last_dl_status = d

        # Check if Max Video Downloads already reached
        if len(self.videos_downloaded_from_url) != 0 and len(self.videos_downloaded_from_url) >= self.max_videos_per_url:
            # DO NOT RAISE ON 0! (22-12-8 max_videos_per_url should no longer ever be 0)
            raise MaxVideosDownloaded('Max videos for URL reached.')

        # Make sure we can stop downloads
        if self.interrupted:
            raise ProcessorInterruptedException("Interrupted while downloading videos.")

    def yt_dlp_post_monitor(self, d):
        """
        Can be used to gather information from yt-dlp while post processing the downloads
        """
        self.last_post_process_status = d
        if d['status'] == 'finished':  # "downloading", "error", or "finished"
            self.videos_downloaded_from_url.add(d.get('info_dict',{}).get('_filename', {}))
            self.url_files[d.get('info_dict',{}).get('_filename', {})] = {
                "filename": Path(d.get('info_dict').get('_filename')).name,
                "metadata": d.get('info_dict'),
                "success": True
            }

        # Make sure we can stop downloads
        if self.interrupted:
            raise ProcessorInterruptedException("Interrupted while downloading videos.")

    def download_video_with_requests(self, url, results_path, max_video_size, retries=0):
        """
        Download a video with the Python requests library

        :param str url:             Valid URL direct to video source
        :param results_path:        Path to location for video download
        :param int max_video_size:  Maximum size in Bytes for video; 0 allows any size
        :param int retries:         Current number of retries to request video
        :return str:  File name     Returns file name of the video after download
        """
        if retries > 1:
            # Currently, only allow 1 retry with newly formatted URL via InvalidSchema/MissingSchema exception
            raise FailedDownload('Retries exceeded')
        # Open stream
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:107.0) Gecko/20100101 Firefox/107.0"
        try:
            with requests.get(url, stream=True, timeout=20, headers={"User-Agent": user_agent}) as response:
                if 400 <= response.status_code < 500:
                    raise FailedDownload(
                        f"Website denied download request (Code {response.status_code} / Reason {response.reason}): {url}")
                elif response.status_code != 200:
                    raise FailedDownload(f"Unable to obtain URL (Code {response.status_code} / Reason {response.reason}): {url}")

                # Verify video
                # YT-DLP will download images; so we raise them differently
                # TODO: test/research other possible ways to verify video links; watch for additional YT-DLP oddities
                content_type = response.headers.get("Content-Type")
                if not content_type:
                    raise VideoStreamUnavailable(f"Unable to verify video; no Content-Type provided: {url}")
                elif "image" in content_type.lower():
                    raise NotAVideo("Not a Video (%s): %s" % (response.headers["Content-Type"], url))
                elif "video" not in content_type.lower():
                    raise VideoStreamUnavailable(f"Does not appear to be a direct to video link: {url}; "
                                                 f"Content-Type: {response.headers['Content-Type']}")

                extension = response.headers["Content-Type"].split("/")[-1]
                # DEBUG Content-Type
                if extension not in ["mp4", "mp3"]:
                    self.dataset.log(f"DEBUG: Odd extension type {extension}; Notify 4CAT maintainers if video. "
                                     f"Content-Type for url {url}: {response.headers['Content-Type']}")

                # Ensure unique filename
                unique_filename = url_to_filename(url, staging_area=results_path, default_ext="."+extension)
                save_location = results_path.joinpath(unique_filename)

                # Check video size (after ensuring it is actually a video above)
                if not max_video_size == 0:
                    if response.headers.get("Content-Length", False):
                        if int(response.headers.get("Content-Length")) > (max_video_size * 1000000):  # Use Bytes!
                            raise FilesizeException(
                                f"Video size {response.headers.get('Content-Length')} larger than maximum allowed per 4CAT")
                    # Size unknown
                    elif not self.config.get("video-downloader.allow-unknown-size", False):
                        raise FilesizeException("Video size unknown; not allowed to download per 4CAT settings")

                # Download video
                self.dataset.update_status(
                    "Downloading %i/%i via requests: %s" % (self.downloaded_videos + 1, self.total_possible_videos, url))
                with open(results_path.joinpath(save_location), "wb") as f:
                    try:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not max_video_size == 0 and f.tell() > (max_video_size * 1000000):
                                    # File size too large; stop download and remove file
                                    os.remove(f.name)
                                    raise FilesizeException("Video size larger than maximum allowed per 4CAT")
                            if chunk:
                                f.write(chunk)
                    except requests.exceptions.ChunkedEncodingError as e:
                        raise FailedDownload(f"Failed to complete download: {e}")

                # Return filename to add to metadata
                return save_location.name
        except (requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema):
            # Reformat URLs that are missing or have invalid schema
            return self.download_video_with_requests('https://' + url.lstrip(' :/'), results_path, max_video_size, retries=retries+1)

    def collect_video_urls(self):
        """
        Extract video URLs from a dataset

        :return dict:  Dict with URLs as keys and a dict with a "post_ids" key
        as value
        """
        urls = {}
        columns = self.parameters.get("columns")
        if type(columns) is str:
            columns = [columns]

        if not columns:
            raise ProcessorException("No columns selected; cannot collect video urls.")

        self.dataset.update_status("Reading source file")
        for index, post in enumerate(self.source_dataset.iterate_items(self)):
            item_urls = set()
            if index + 1 % 250 == 0:
                self.dataset.update_status(
                    "Extracting video links from item %i/%i" % (index + 1, self.source_dataset.num_rows))

            # loop through all columns and process values for item
            for column in columns:
                value = post.get(column)
                if not value:
                    continue

                if value is not str:
                    value = str(value)

                video_links = self.identify_video_urls_in_string(value)
                if video_links:
                    item_urls |= set(video_links)

            for item_url in item_urls:
                if item_url not in urls:
                    urls[item_url] = {'post_ids': {post.get('id')}}
                else:
                    urls[item_url]['post_ids'].add(post.get('id'))

        if not urls:
            raise ProcessorException("No video urls identified in provided data.")
        else:
            return urls

    def identify_video_urls_in_string(self, text):
        """
        Search string of text for URLs that may contain video links.

        :param str text:  string that may contain URLs
        :return list:  	  list containing validated URLs to videos
        """
        split_comma = self.parameters.get("split-comma", True)
        if split_comma:
            texts = text.split(",")
        else:
            texts = [text]

        # Currently extracting all links
        urls = set()
        for string in texts:
            urls |= set([url for url in urls_from_text(string)])
        return list(urls)

    def copy_previous_video(self, previous_vid_metadata, staging_area, previous_downloaders):
        """
        Copy existing video to new staging area
        """
        num_copied = 0
        dataset_key = previous_vid_metadata.get("file_dataset_key")
        dataset = [dataset for dataset in previous_downloaders if dataset.key == dataset_key]

        if "files" in previous_vid_metadata:
            files = previous_vid_metadata.get('files')
        elif "filename" in previous_vid_metadata:
            files = [{"filename": previous_vid_metadata.get("filename"), "success": True}]
        else:
            raise FailedToCopy("Unable to read video metadata")

        if not files:
            raise FailedToCopy("No file found in metadata")

        if not dataset:
            raise FailedToCopy(f"Dataset with key {dataset_key} not found")
        else:
            dataset = dataset[0]

        with zipfile.ZipFile(dataset.get_results_path(), "r") as archive_file:
            archive_contents = sorted(archive_file.namelist())

            for file in files:
                if file.get("filename") not in archive_contents:
                    raise FailedToCopy(f"Previously downloaded video {file.get('filename')} not found")

                self.dataset.log(f"Copying previously downloaded video {file.get('filename')} to new staging area")
                archive_file.extract(file.get("filename"), staging_area)
                num_copied += 1

        return num_copied


    @staticmethod
    def map_metadata(url, data):
        """
        Iterator to yield modified metadata for CSV

        :param str url:  string that may contain URLs
        :param dict data:  dictionary with metadata collected previously
        :yield dict:  	  iterator containing reformated metadata
        """
        row = {
            "url": url,
            "number_of_posts_with_url": len(data.get("post_ids", [])),
            "post_ids": ", ".join(data.get("post_ids", [])),
            "downloader": data.get("downloader", ""),
            "download_successful": data.get('success', "")
        }

        for file in data.get("files", [{}]):
            row["filename"] = file.get("filename", "N/A")

            yt_dlp_data = file.get("metadata", {})
            for common_column in ["title", "artist", "description", "view_count", "like_count", "repost_count", "comment_count", "uploader", "creator", "uploader_id"]:
                if yt_dlp_data:
                    row[f"extracted_{common_column}"] = yt_dlp_data.get(common_column)
                else:
                    row[f"extracted_{common_column}"] = "N/A"

            row["error"] = data.get("error", "N/A")

            yield row


class DatasetVideoLibrary:

    def __init__(self, current_dataset):
        self.current_dataset = current_dataset
        self.previous_downloaders = self.collect_previous_downloaders()
        self.current_dataset.log(f"Previously video downloaders: {[downloader.key for downloader in self.previous_downloaders]}")

        metadata_files = self.collect_all_metadata_files()

        # Build library
        library = {}
        for metadata_file in metadata_files:
            for url, data in metadata_file[1].items():
                if data.get("success", False):
                    # Always overwrite for success
                    library[url] = {
                        **data,
                        "file_dataset_key": metadata_file[0]
                    }
                elif url not in library:
                    # Do not overwrite failures, but do add if missing
                    library[url] = {
                        **data,
                        "file_dataset_key": metadata_file[0]
                    }

        self.current_dataset.log(f"Total URLs previously seen: {len(library)}")

        self.library = library

    def collect_previous_downloaders(self):
        """
        Check for other video-downloader processors run on the dataset and create library for reference
        """
        #TODO: recursive this so we can check downloads from other filters? NO, make a central point and use the ORIGINAL key

        parent_dataset = self.current_dataset.get_parent()
        # Note: exclude current dataset
        previous_downloaders = [child for child in parent_dataset.get_children() if (child.type == "video-downloader" and child.key != self.current_dataset.key)]

        # Check to see if filtered dataset
        if "copied_from" in parent_dataset.parameters and parent_dataset.is_top_dataset():
            try:
                original_dataset = DataSet(key=parent_dataset.parameters["copied_from"], db=self.current_dataset.db)
                previous_downloaders += [child for child in original_dataset.top_parent().get_children() if
                                         (child.type == "video-downloader" and child.key != self.current_dataset.key)]
            except DataSetException:
                # parent dataset no longer exists!
                pass

        return previous_downloaders

    def collect_metadata_file(self, dataset, staging_area):
        source_file = dataset.get_results_path()
        if not source_file.exists():
            return None

        with zipfile.ZipFile(dataset.get_results_path(), "r") as archive_file:
            archive_contents = sorted(archive_file.namelist())
            if '.metadata.json' not in archive_contents:
                return None

            archive_file.extract(".metadata.json", staging_area)

            with open(staging_area.joinpath(".metadata.json")) as file:
                return json.load(file)

    def collect_all_metadata_files(self):
        import shutil
        metadata_staging_area = self.current_dataset.get_staging_area()

        metadata_files = [(downloader.key, self.collect_metadata_file(downloader, metadata_staging_area)) for downloader in self.previous_downloaders]
        metadata_files = [file for file in metadata_files if file[1] is not None]
        self.current_dataset.log(f"Metadata files collected: {len(metadata_files)}; with {[len(urls[0]) for urls in metadata_files]}")

        # Delete staging area
        shutil.rmtree(metadata_staging_area)

        return metadata_files


class MaxVideosDownloaded(ProcessorException):
    """
    Raise if too many videos have been downloaded and the processor should stop future downloads
    """
    pass


class FailedDownload(ProcessorException):
    """
    Raise if Download failed and will not be tried again
    """
    pass


class VideoStreamUnavailable(ProcessorException):
    """
    Raise request stream does not contain video, BUT URL may be able to be processed by YT-DLP
    """
    pass


class NotAVideo(ProcessorException):
    """
    Raise if we know URL does not contain video OR URL that YT-DLP can handle
    """
    pass


class FilesizeException(ProcessorException):
    """
    Raise if video size does not meet criteria
    """
    pass


class LiveVideoException(ProcessorException):
    """
    Raise if live videos are not allowed
    """
    pass


class FailedToCopy(ProcessorException):
    """
    Raise if unable to copy video from previous dataset
    """
    pass
