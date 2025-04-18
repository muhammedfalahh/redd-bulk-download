import subprocess
from bs4 import BeautifulSoup
import coloredlogs
from colorama import Fore
import contextlib
import logging
import verboselogs
from datetime import datetime
import os
from io import StringIO
import json
import mimetypes
import ffmpeg # Note: The previous 'download_reddit_video' used os.system for ffmpeg. If you prefer the python-ffmpeg library, ensure its usage is correct. The provided update uses os.system.
import praw
from pprint import pprint
import re
import requests
import urllib3
from tqdm import tqdm
import urllib.request
import youtube_dl
import os


class SubmissionDownloader:
    def __init__(self, submission, submission_index, logger, output_dir, skip_videos, skip_meta, skip_comments, comment_limit, config):
        # Ensure config is a dictionary and has the necessary key
        if not isinstance(config, dict):
             raise TypeError("Config must be a dictionary.")
        self.IMGUR_CLIENT_ID = config.get("imgur_client_id") # Use .get for safer access
        if not self.IMGUR_CLIENT_ID:
             # Handle missing client ID, e.g., raise error or log warning
             # For now, let's assume it might be optional for some operations
             # logger.warning("Imgur Client ID not found in config. Imgur Album/Image downloads might fail.")
             pass # Or raise ValueError("Missing 'imgur_client_id' in config")

        self.logger = logger
        i = submission_index
        prefix_str = '#' + str(i).zfill(3) + ' '
        self.indent_1 = ' ' * len(prefix_str) + "* "
        self.indent_2 = ' ' * len(self.indent_1) + "- "

        has_url = getattr(submission, "url", None)
        if has_url:
            title = submission.title
            self.logger.verbose(prefix_str + '"' + title + '"')
            # Sanitize title for filesystem compatibility
            # Replace non-alphanumeric characters (except underscore/hyphen) with underscore
            title = re.sub(r'[^\w\-]+', '_', title)
            # Remove leading/trailing underscores/spaces
            title = title.strip('_ ')

            # Truncate title - Be careful with filesystem limits (e.g., 255 chars on many systems)
            # 32 is very short, maybe increase slightly? Consider the full path length too.
            max_title_len = 64 # Increased from 32
            if len(title) > max_title_len:
                title = title[:max_title_len]
                # Adding ellipsis might not be ideal for directory names
                # title += "..."

            # Prepare directory for the submission
            post_dir = str(i).zfill(3) + "_" + title # Removed .replace(" ", "_") as spaces are already handled
            submission_dir = os.path.join(output_dir, post_dir)

            # Check existence *before* creating
            if os.path.exists(submission_dir):
                # Use logger instead of print for consistency
                self.logger.notice(f"Directory '{submission_dir}' already exists, skipping submission.")
                return # Skip this submission entirely if the main dir exists

            # Create the directory *after* the check
            try:
                os.makedirs(submission_dir)
            except OSError as e:
                self.logger.error(f"Failed to create directory {submission_dir}: {e}")
                return # Cannot proceed if directory creation fails


            self.logger.spam(
                self.indent_1 + "Processing `" + submission.url + "`")

            success = False # Tracks if *any* download succeeded for logging purposes

            should_create_files_dir = True
            if skip_comments and skip_meta:
                should_create_files_dir = False

            # --- Inner Function: create_files_dir ---
            # Moved definition here for clarity before first use
            def create_files_dir(submission_dir):
                files_dir = submission_dir # Default to submission_dir if not creating subfolder
                if should_create_files_dir:
                    target_dir = os.path.join(submission_dir, "files")
                    if not os.path.exists(target_dir):
                        try:
                            os.makedirs(target_dir)
                            files_dir = target_dir # Update files_dir only on successful creation
                        except OSError as e:
                             self.logger.error(f"Failed to create 'files' subdirectory in {submission_dir}: {e}")
                             # Decide how to handle this: maybe download to submission_dir? For now, log and continue.
                    else:
                        files_dir = target_dir # Use existing 'files' dir
                return files_dir
            # --- End Inner Function ---


            # --- Content Type Handling ---
            # Using a more structured if/elif/else approach

            # 1. Direct Links (Images/MP4)
            if self.is_direct_link_to_content(submission.url, [".png", ".jpg", ".jpeg", ".gif"]):
                files_dir = create_files_dir(submission_dir)
                filename = os.path.basename(urllib.parse.urlparse(submission.url).path) # Safer filename extraction
                if not filename: filename = f"{submission.id}_image" # Fallback filename
                self.logger.spam(
                    self.indent_1 + "This is a direct link to an image file (" + filename + ")")
                save_path = os.path.join(files_dir, filename)
                if self.download_direct_link(submission, save_path):
                    success = True

            elif self.is_direct_link_to_content(submission.url, [".mp4"]):
                filename = os.path.basename(urllib.parse.urlparse(submission.url).path)
                if not filename: filename = f"{submission.id}_video.mp4"
                self.logger.spam(
                    self.indent_1 + "This is a direct link to an MP4 file (" + filename + ")")
                if not skip_videos:
                    files_dir = create_files_dir(submission_dir)
                    save_path = os.path.join(files_dir, filename)
                    if self.download_direct_link(submission, save_path):
                        success = True
                else:
                    self.logger.spam(self.indent_1 + "Skipping download of video content")
                    success = True # Mark success as we intentionally skipped

            # 2. Reddit Gallery
            elif self.is_reddit_gallery(submission.url):
                 files_dir = create_files_dir(submission_dir)
                 self.logger.spam(self.indent_1 + "This is a reddit gallery")
                 if self.download_reddit_gallery(submission, files_dir, skip_videos):
                     success = True

            # 3. Reddit Video
            elif self.is_reddit_video(submission.url):
                self.logger.spam(self.indent_1 + "This is a reddit video")
                if not skip_videos:
                    files_dir = create_files_dir(submission_dir)
                    # download_reddit_video now handles its own success logging internally
                    self.download_reddit_video(submission, files_dir)
                    # We might consider success=True even if audio fails but video downloads?
                    # For now, let's assume download_reddit_video handles logging failure.
                    # If the function completes without error, assume success.
                    success = True # Assuming the function handles internal errors and logging
                else:
                    self.logger.spam(self.indent_1 + "Skipping download of video content")
                    success = True

            # 4. Gfycat / Redgifs
            elif self.is_gfycat_link(submission.url) or self.is_redgifs_link(submission.url):
                link_type = "gfycat" if self.is_gfycat_link(submission.url) else "redgif"
                self.logger.spam(self.indent_1 + f"This is a {link_type} link")
                if not skip_videos:
                    files_dir = create_files_dir(submission_dir)
                    if self.download_gfycat_or_redgif(submission, files_dir):
                         success = True
                else:
                    self.logger.spam(self.indent_1 + "Skipping download of video content")
                    success = True

            # 5. Imgur Album
            elif self.is_imgur_album(submission.url):
                # Check if Imgur Client ID is available
                if not self.IMGUR_CLIENT_ID:
                    self.logger.warning(self.indent_1 + "Skipping Imgur album download: Imgur Client ID not configured.")
                else:
                    files_dir = create_files_dir(submission_dir)
                    self.logger.spam(self.indent_1 + "This is an imgur album")
                    if self.download_imgur_album(submission, files_dir):
                        success = True

            # 6. Imgur Image/Video
            elif self.is_imgur_image(submission.url): # Should be checked *after* album
                 # Check if Imgur Client ID is available
                 if not self.IMGUR_CLIENT_ID:
                     self.logger.warning(self.indent_1 + "Skipping Imgur image/video download: Imgur Client ID not configured.")
                 else:
                     files_dir = create_files_dir(submission_dir)
                     self.logger.spam(self.indent_1 + "This is an imgur image or video")
                     if self.download_imgur_image(submission, files_dir):
                         success = True

            # 7. Self Post
            elif self.is_self_post(submission):
                self.logger.spam(self.indent_1 + "This is a self-post (no external media)")
                success = True # Nothing to download, so considered successful

            # 8. YouTube-DL Supported (including YouTube)
            elif (not skip_videos) and (self.is_youtube_link(submission.url) or self.is_supported_by_youtubedl(submission.url)):
                link_type = "youtube" if self.is_youtube_link(submission.url) else "youtube-dl supported"
                self.logger.spam(self.indent_1 + f"This is a {link_type} link")
                # No need for inner 'if not skip_videos' as it's already checked
                files_dir = create_files_dir(submission_dir)
                if self.download_youtube_video(submission.url, files_dir):
                    success = True
            elif skip_videos and (self.is_youtube_link(submission.url) or self.is_supported_by_youtubedl(submission.url)):
                 self.logger.spam(self.indent_1 + "Skipping download of video content (youtube-dl)")
                 success = True

            # 9. Fallback / Unknown
            else:
                self.logger.notice(self.indent_1 + f"Link type not explicitly handled or skipped: {submission.url}")
                # Optionally try a generic download attempt here? Or just mark as success if metadata/comments are saved.
                success = True # Consider it success if we just save meta/comments


            # --- Metadata and Comments ---
            if not skip_meta:
                self.logger.spam(self.indent_1 + "Saving submission.json")
                self.download_submission_meta(submission, submission_dir)
            else:
                self.logger.spam(self.indent_1 + "Skipping submission meta")

            if not skip_comments:
                limit_desc = "all" if comment_limit is None else f"top-level (limit={comment_limit})"
                self.logger.spam(self.indent_1 + f"Saving {limit_desc} comments to comments.json")
                self.download_comments(submission, submission_dir, comment_limit)
            else:
                self.logger.spam(self.indent_1 + "Skipping comments")

            # --- Final Logging ---
            if success:
                 # Log success only if directory was actually created (avoid logging for skipped existing dirs)
                 if os.path.exists(submission_dir): # Double check it wasn't skipped early on
                      self.logger.spam(self.indent_1 + "Saved to " + submission_dir + "\n")
            else:
                 # Only log failure if we didn't skip due to existing directory
                 if os.path.exists(submission_dir):
                    self.logger.warning(
                        self.indent_1 + "Potentially failed to download content from link " + submission.url + "\n"
                    )
                    # Consider removing the created directory if the download failed completely?
                    # Might be risky if metadata/comments *did* save.
                    # Example cleanup (use with caution):
                    # if not skip_meta and not skip_comments and not os.listdir(files_dir): # If files dir is empty and meta/comments weren't skipped
                    #    try:
                    #        shutil.rmtree(submission_dir)
                    #        self.logger.warning(self.indent_1 + f"Removed empty directory due to download failure: {submission_dir}")
                    #    except Exception as e:
                    #        self.logger.error(self.indent_1 + f"Failed to remove directory {submission_dir}: {e}")


        else: # No URL attribute found
            self.logger.warning(f"Submission {submission.id} at index {i} seems to lack a URL attribute. Skipping.")


    def print_formatted_error(self, e):
        # Log multi-line errors properly indented
        error_str = str(e).strip() # Remove leading/trailing whitespace
        for line in error_str.splitlines(): # Use splitlines to handle different newline chars
            self.logger.error(self.indent_2 + line)

    def is_direct_link_to_content(self, url, supported_file_formats):
        # Basic check, might need refinement for URLs with query params etc.
        # Consider using urllib.parse
        try:
            path = urllib.parse.urlparse(url).path
            filename = os.path.basename(path)
            if not filename: return False # No filename part in URL path
            # Check extension
            return any(filename.lower().endswith(ext) for ext in supported_file_formats) and ".gifv" not in filename.lower()
        except Exception as e:
            self.logger.error(f"Error parsing URL {url} in is_direct_link_to_content: {e}")
            return False

    def download_direct_link(self, submission, output_path):
        # Returns True on success, False on failure
        try:
            # Use requests for better error handling and headers
            headers = {'User-Agent': 'SavedditDownloader/1.0'} # Be a good internet citizen
            response = requests.get(submission.url, stream=True, headers=headers, timeout=30) # Added timeout
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            total_size = int(response.headers.get('content-length', 0))
            block_size = 1024 # 1 Kibibyte

            # Use tqdm for progress bar
            with open(output_path, 'wb') as file, tqdm(
                    desc=os.path.basename(output_path),
                    total=total_size,
                    unit='iB',
                    unit_scale=True,
                    unit_divisor=1024,
                    bar_format='%s%s{l_bar}{bar:20}{r_bar}%s' % (self.indent_2, Fore.WHITE + Fore.LIGHTBLACK_EX, Fore.RESET),
                    leave=False # Don't leave completed bar behind if logging many files
                ) as bar:
                for data in response.iter_content(block_size):
                    bar.update(len(data))
                    file.write(data)

            # Check if download was complete (optional but good practice)
            if total_size != 0 and bar.n != total_size:
                 self.logger.warning(self.indent_2 + f"Downloaded size mismatch for {os.path.basename(output_path)}. Expected {total_size}, got {bar.n}")
                 # Decide if this constitutes failure - for now, let's say no unless raise_for_status failed
            self.logger.spam(self.indent_2 + f"Successfully downloaded {os.path.basename(output_path)}")
            return True

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to download direct link: {submission.url}")
            self.print_formatted_error(e)
            # Clean up potentially incomplete file
            if os.path.exists(output_path):
                try: os.remove(output_path)
                except OSError: pass
            return False
        except Exception as e: # Catch other potential errors
            self.logger.error(self.indent_2 + f"An unexpected error occurred downloading direct link: {submission.url}")
            self.print_formatted_error(e)
            if os.path.exists(output_path):
                try: os.remove(output_path)
                except OSError: pass
            return False


    def is_youtube_link(self, url):
        # More robust check
        try:
            parsed_url = urllib.parse.urlparse(url.lower())
            return parsed_url.netloc in ('www.youtube.com', 'youtube.com', 'youtu.be', 'm.youtube.com')
        except Exception:
            return False # Handle potential parsing errors

    def is_supported_by_youtubedl(self, url):
        # This check can be slow as it initializes YoutubeDL each time.
        # Consider optimizing if performance is critical (e.g., cache results or use regex for common sites)
        # Current implementation is functional but potentially inefficient.
        try:
            # Silence youtube-dl's excessive output
            # Note: redirect_stderr might not capture everything ytdl prints depending on its internals.
            # Using quiet=True is often sufficient.
            local_stderr = StringIO()
            with contextlib.redirect_stderr(local_stderr): # Keep redirecting stderr if needed
                if "flickr.com/photos" in url: # Keep specific exclusions if necessary
                    return False

                # Use a more minimal set of options for info extraction
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True, # Suppress warnings too
                    'ignoreerrors': True,
                    'extract_flat': True, # Faster, gets list without resolving formats
                    'skip_download': True,
                }
                # Use try-except block specifically for ytdl operations
                try:
                    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                        # extract_info will return None or raise error if not supported/found
                        info_dict = ydl.extract_info(url, download=False)
                        if info_dict and info_dict.get('extractor_key') and info_dict.get('extractor_key') != 'Generic':
                             self.logger.spam(self.indent_2 + f"URL potentially supported by youtube-dl (Extractor: {info_dict.get('extractor_key')})")
                             return True
                        else:
                             # Check stderr for specific messages if needed, but usually info_dict is enough
                             # error_output = local_stderr.getvalue()
                             # if "Unsupported URL" in error_output: ...
                             self.logger.spam(self.indent_2 + f"youtube-dl did not find a specific extractor for '{url}'")
                             return False

                except youtube_dl.utils.DownloadError as e:
                    # Specific ytdl download errors (like unsupported URL) often land here
                    self.logger.spam(self.indent_2 + f"youtube-dl check failed for '{url}': {e}")
                    return False
                except Exception as e:
                    # Catch other unexpected errors during ytdl processing
                    self.logger.error(self.indent_2 + f"Unexpected error during youtube-dl check for '{url}'")
                    self.print_formatted_error(e)
                    return False

        except Exception as e: # Catch errors in the setup/contextlib part
            self.logger.error(self.indent_2 + "Error setting up youtube-dl check")
            self.print_formatted_error(e)
            return False


    def download_youtube_video(self, url, output_path):
        # Returns True on success, False on failure
        try:
            # Define options *outside* the context manager is slightly cleaner
            download_options = {
                 # Prefer better quality MP4 directly if available (common for YouTube)
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
                'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'), # Use os.path.join
                'quiet': True,
                # 'verbose': True, # Uncomment for debugging ytdl issues
                'no_warnings': True,
                'ignoreerrors': True, # Continue processing even if one video in a playlist fails
                'nooverwrites': True, # Don't redownload if file exists
                'continuedl': True, # Resume partial downloads
                # Consider adding progress hook for tqdm integration if desired
                # 'progress_hooks': [my_hook],
            }

            self.logger.spam(self.indent_2 + f"Attempting download: {url} with youtube-dl")

            # Capture stderr/stdout more directly if needed for errors
            log_stream = StringIO()
            # Need contextlib to redirect stdout as well potentially
            with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
                 with youtube_dl.YoutubeDL(download_options) as ydl:
                    try:
                         result = ydl.download([url])
                         # result code 0 typically means success, but ytdl might return other codes.
                         # Checking for errors in log_stream is often more reliable.
                         output_log = log_stream.getvalue().strip()

                         if "ERROR:" in output_log or result != 0 : # Check for ERROR string and non-zero result
                            self.logger.error(self.indent_2 + f"youtube-dl encountered an error for {url}:")
                            # Print relevant parts of the log
                            for line in output_log.splitlines():
                                if "ERROR:" in line or "WARNING:" in line: # Show errors and warnings
                                     self.logger.error(self.indent_2 + "  " + line)
                            return False
                         else:
                             self.logger.spam(self.indent_2 + f"Finished youtube-dl download for {url}")
                             # Check if any file was actually created (ytdl might 'succeed' with no download if file exists and nooverwrites=True)
                             # This part is tricky, requires knowing the expected output filename.
                             # For simplicity, assume success if no error was logged.
                             return True

                    except youtube_dl.utils.DownloadError as e:
                         self.logger.error(self.indent_2 + f"youtube-dl download failed for {url}.")
                         # Error might be printed directly by ytdl, or available in e
                         self.print_formatted_error(e)
                         # Print captured log as well
                         output_log = log_stream.getvalue().strip()
                         if output_log:
                              self.logger.error(self.indent_2 + "Captured youtube-dl output:")
                              for line in output_log.splitlines():
                                   self.logger.error(self.indent_2 + "  " + line)
                         return False
                    except Exception as e: # Catch unexpected errors within ydl.download
                         self.logger.error(self.indent_2 + f"Unexpected error during youtube-dl download call for {url}.")
                         self.print_formatted_error(e)
                         return False

        except Exception as e: # Catch errors during YoutubeDL instantiation or context setup
            self.logger.error(self.indent_2 + f"Failed to initialize or run youtube-dl for {url}")
            self.print_formatted_error(e)
            return False


    def is_reddit_gallery(self, url):
        return "reddit.com/gallery/" in url

    def download_reddit_gallery(self, submission, output_path, skip_videos):
        # Returns True if successful (or skipped), False on major error
        gallery_data = None
        media_metadata = None

        # Try getting data directly from submission
        try:
            if hasattr(submission, "gallery_data") and hasattr(submission, "media_metadata"):
                gallery_data = submission.gallery_data
                media_metadata = submission.media_metadata
                self.logger.spam(self.indent_2 + "Found gallery_data and media_metadata on submission.")
            elif hasattr(submission, "crosspost_parent_list") and submission.crosspost_parent_list:
                # Check crosspost parent if direct attributes are missing
                self.logger.spam(self.indent_2 + "Trying gallery data from crosspost parent.")
                # PRAW loads crosspost parent data as dicts, not Submission objects
                parent_data = submission.crosspost_parent_list[0]
                if "gallery_data" in parent_data and "media_metadata" in parent_data:
                    gallery_data = parent_data["gallery_data"]
                    media_metadata = parent_data["media_metadata"]
                    self.logger.spam(self.indent_2 + "Found gallery data in crosspost parent.")

        except Exception as e:
             self.logger.error(self.indent_2 + "Error accessing gallery data attributes.")
             self.print_formatted_error(e)
             return False # Cannot proceed without the data

        # Proceed if data was found
        if gallery_data and media_metadata and 'items' in gallery_data:
            items = gallery_data["items"]
            image_count = len(items)
            self.logger.spam(self.indent_2 + f"Reddit gallery has {image_count} item(s)")

            if not items:
                 self.logger.warning(self.indent_2 + "Gallery data found, but 'items' list is empty.")
                 return True # No items to download, consider it success

            success_count = 0
            for j, item in tqdm(enumerate(items), total=image_count, bar_format='%s%s{l_bar}{bar:20}{r_bar}%s' % (self.indent_2, Fore.WHITE + Fore.LIGHTBLACK_EX, Fore.RESET), leave=False):
                try:
                    media_id = item.get("media_id")
                    if not media_id:
                        self.logger.warning(self.indent_2 + f"Item {j+1} missing media_id, skipping.")
                        continue

                    item_meta = media_metadata.get(media_id)
                    if not item_meta:
                         self.logger.warning(self.indent_2 + f"Metadata not found for media_id {media_id}, skipping item {j+1}.")
                         continue

                    # Determine type and URL
                    item_mimetype = item_meta.get('m') # e.g., 'image/jpg', 'video/mp4'
                    item_source_info = item_meta.get('s') # Contains 'u' (URL) or 'gif' (gif URL) or 'mp4' (video URL)

                    item_url = None
                    file_ext = None

                    if item_source_info:
                        if 'u' in item_source_info and item_mimetype and 'image' in item_mimetype:
                            item_url = item_source_info['u']
                            file_ext = mimetypes.guess_extension(item_mimetype) or f".{item_mimetype.split('/')[-1]}"
                        elif 'mp4' in item_source_info and item_mimetype and 'video' in item_mimetype:
                             item_url = item_source_info['mp4']
                             file_ext = '.mp4'
                        elif 'gif' in item_source_info and item_mimetype and 'image' in item_mimetype: # Animated gifs might be listed this way
                             item_url = item_source_info['gif']
                             file_ext = '.gif'
                        elif 'u' in item_source_info: # Fallback if mimetype is weird but URL exists
                             item_url = item_source_info['u']
                             # Try guessing extension from URL or default to .jpg
                             possible_ext = os.path.splitext(urllib.parse.urlparse(item_url).path)[1]
                             file_ext = possible_ext if possible_ext else '.jpg'
                             self.logger.warning(f"Using fallback URL 'u' for media_id {media_id}, guessed extension: {file_ext}")


                    if not item_url or not file_ext:
                         self.logger.warning(self.indent_2 + f"Could not determine URL or extension for media_id {media_id}, skipping item {j+1}.")
                         # pprint(item_meta, indent=2) # Uncomment to debug metadata structure
                         continue

                    # Handle video skipping
                    if 'video' in (item_mimetype or "") and skip_videos:
                        self.logger.spam(self.indent_2 + f"Skipping video item {j+1} ({media_id}).")
                        success_count += 1 # Count skipped as success for gallery overall status
                        continue

                    # Construct filename and save path
                    # Use index j for ordering + media_id for uniqueness
                    item_filename = f"{str(j).zfill(3)}_{media_id}{file_ext}"
                    save_path = os.path.join(output_path, item_filename)

                    # Download the item
                    try:
                        # Use requests for gallery items too
                        headers = {'User-Agent': 'SavedditDownloader/1.0'}
                        response = requests.get(item_url, stream=True, headers=headers, timeout=20)
                        response.raise_for_status()
                        with open(save_path, 'wb') as f:
                            for chunk in response.iter_content(1024 * 8): # 8KB chunks
                                f.write(chunk)
                        success_count += 1
                    except requests.exceptions.RequestException as download_err:
                        self.logger.error(self.indent_2 + f"Failed to download gallery item {j+1} ({media_id}) from {item_url}")
                        self.print_formatted_error(download_err)
                        # Clean up partial file
                        if os.path.exists(save_path):
                             try: os.remove(save_path)
                             except OSError: pass
                    except Exception as e:
                         self.logger.error(self.indent_2 + f"Unexpected error downloading gallery item {j+1} ({media_id})")
                         self.print_formatted_error(e)
                         if os.path.exists(save_path):
                              try: os.remove(save_path)
                              except OSError: pass

                except Exception as item_proc_err:
                    # Catch errors processing a single item's data
                    self.logger.error(self.indent_2 + f"Error processing gallery item {j+1}")
                    self.print_formatted_error(item_proc_err)
                    # Continue to the next item

            # Log final status for the gallery
            if success_count == image_count:
                 self.logger.spam(self.indent_2 + f"Successfully processed all {image_count} gallery items.")
            elif success_count > 0:
                 self.logger.warning(self.indent_2 + f"Successfully processed {success_count} out of {image_count} gallery items.")
            else:
                 self.logger.error(self.indent_2 + f"Failed to process any items in the gallery.")
            return success_count > 0 # Return True if at least one item succeeded (or was skipped)

        else:
            # Handle case where gallery_data or media_metadata couldn't be found/parsed
            self.logger.error(self.indent_2 + "Could not find valid gallery data for this submission.")
            return False


    def is_reddit_video(self, url):
        # Check if the URL is specifically a v.redd.it link
        try:
             return urllib.parse.urlparse(url).netloc == 'v.redd.it'
        except Exception:
             return False

    # --- START UPDATED download_reddit_video ---
    def download_reddit_video(self, submission, output_path):
        media = getattr(submission, "media", None)
        # Extract media_id robustly from submission.url
        match = re.search(r"v\.redd\.it/([^/?#]+)", submission.url)
        if not match:
            self.logger.error(self.indent_2 + f"Could not extract media ID from URL: {submission.url}")
            return # Cannot proceed without media_id
        media_id = match.group(1)
        self.logger.spam(self.indent_2 + f"Extracted media ID: {media_id}")


        self.logger.spam(self.indent_2 + "Looking for submission.media")

        if media is None:
            # Link might be a crosspost
            crosspost_parent_list = getattr(submission, "crosspost_parent_list", None)
            if crosspost_parent_list and len(crosspost_parent_list) > 0:
                self.logger.spam(self.indent_2 + "This is a crosspost, checking parent for media.")
                # PRAW loads parent data as dict
                first_parent = crosspost_parent_list[0]
                media = first_parent.get("media") # Use .get for safety
                if media:
                    self.logger.spam(self.indent_2 + "Found media data in crosspost parent.")
                else:
                    self.logger.warning(self.indent_2 + "Crosspost parent data does not contain 'media'.")
                    # Attempt to re-fetch? Risky, could lead to rate limits or infinite loops.
                    # Best to rely on the initially fetched data.
                    # If PRAW didn't include 'media' initially, it's likely missing.
                    # Consider if the parent post itself was deleted or had no media.
                    # submission = reddit_instance.submission(id=first_parent['id']) # Requires reddit instance
                    # media = getattr(submission, "media", None)
            else:
                 self.logger.warning(self.indent_2 + "Submission media is None and it's not a recognized crosspost or parent lacks media.")
                 return # Cannot proceed without media data

        # Check if media dictionary and reddit_video sub-dictionary exist
        if media is not None and isinstance(media, dict) and 'reddit_video' in media:
            reddit_video_data = media['reddit_video']
            if not isinstance(reddit_video_data, dict):
                 self.logger.error(self.indent_2 + "media['reddit_video'] is not a dictionary. Cannot process.")
                 return

            video_url = reddit_video_data.get('fallback_url') # Use .get for safety

            if not video_url:
                # Sometimes HLS URL is available but fallback isn't
                hls_url = reddit_video_data.get('hls_url')
                if hls_url:
                     self.logger.warning(self.indent_2 + "fallback_url missing, but hls_url found. Trying to download via youtube-dl (experimental).")
                     # Delegate to youtube-dl which *might* handle HLS manifests
                     if self.download_youtube_video(hls_url, output_path):
                         # If ytdl succeeds, rename the file appropriately
                         try:
                              # ytdl usually names files based on ID, find the downloaded file
                              # This assumes ytdl downloads only one file here.
                              downloaded_files = [f for f in os.listdir(output_path) if os.path.isfile(os.path.join(output_path, f))]
                              # Find a file that likely corresponds to this download (e.g., has media_id in name or is the newest mp4)
                              # This part is fragile. A better approach might involve ytdl hooks.
                              potential_file = None
                              for fname in downloaded_files:
                                   if media_id in fname and fname.endswith(".mp4"): # Basic check
                                       potential_file = os.path.join(output_path, fname)
                                       break
                              # Or assume the newest mp4 if name doesn't match easily
                              if not potential_file:
                                   mp4_files = [os.path.join(output_path, f) for f in downloaded_files if f.endswith('.mp4')]
                                   if mp4_files:
                                        potential_file = max(mp4_files, key=os.path.getctime)


                              if potential_file and os.path.exists(potential_file):
                                   final_path = os.path.join(output_path, media_id + ".mp4")
                                   if potential_file != final_path: # Avoid renaming to self
                                       os.rename(potential_file, final_path)
                                       self.logger.spam(self.indent_2 + f"Renamed youtube-dl output to {final_path}")
                                   return # Successfully handled via youtube-dl
                              else:
                                   self.logger.error(self.indent_2 + "youtube-dl reported success for HLS, but couldn't find output file.")
                                   return # Failed despite ytdl 'success'
                         except Exception as rename_err:
                              self.logger.error(self.indent_2 + f"Error renaming HLS file downloaded via youtube-dl: {rename_err}")
                              return # Failed
                     else:
                          self.logger.error(self.indent_2 + "youtube-dl failed to download from HLS URL.")
                          return # Failed HLS attempt
                else:
                     self.logger.error(self.indent_2 + "Could not find video fallback_url or hls_url in submission media.")
                     return # Cannot proceed without any video URL

            # --- Video Download (from fallback_url) ---
            self.logger.spam(self.indent_2 + f"Downloading video component from: {video_url}")
            video_save_path = os.path.join(output_path, media_id + "_video.mp4")
            try:
                headers = {'User-Agent': 'SavedditDownloader/1.0'}
                response = requests.get(video_url, stream=True, headers=headers, timeout=60) # Increased timeout for potentially large videos
                response.raise_for_status()
                with open(video_save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024): # Larger chunks (1MB) for video
                        f.write(chunk)
                self.logger.spam(self.indent_2 + "Successfully downloaded video component.")
            except requests.exceptions.RequestException as e:
                self.logger.error(self.indent_2 + f"Failed to download video component from {video_url}")
                self.print_formatted_error(e)
                if os.path.exists(video_save_path):
                     try: os.remove(video_save_path)
                     except OSError as rm_err: self.logger.warning(self.indent_2 + f"Could not remove partial video file: {rm_err}")
                return # If video download fails, stop processing this submission's video
            except Exception as e:
                 self.logger.error(self.indent_2 + f"Unexpected error downloading video component from {video_url}")
                 self.print_formatted_error(e)
                 if os.path.exists(video_save_path):
                      try: os.remove(video_save_path)
                      except OSError as rm_err: self.logger.warning(self.indent_2 + f"Could not remove partial video file: {rm_err}")
                 return

            # --- Audio Download ---
            audio_downloaded = False
            audio_save_path = os.path.join(output_path, media_id + "_audio.mp4") # Often mp4 container, but might be .m4a

            # Construct the base URL from the submission URL (most reliable source of media_id)
            # Ensure submission.url ends with media_id and not a slash or query params
            base_vreddit_url = f"https://v.redd.it/{media_id}"
            # Common audio URL patterns (add more if needed)
            # Order matters - try most common first
            audio_url_pattern_1 = f"{base_vreddit_url}/DASH_audio.mp4"
            audio_url_pattern_2 = f"{base_vreddit_url}/DASH_AUDIO_128.mp4" # Found sometimes
            audio_url_pattern_3 = f"{base_vreddit_url}/DASH_AUDIO_64.mp4"  # Found sometimes
            audio_url_pattern_4 = f"{base_vreddit_url}/DASH_audio.m4a"   # Sometimes has m4a extension

            # Try URLs derived from video URL (less reliable but worth a shot if others fail)
            # Replace common resolution indicators + extension
            audio_url_from_video_1 = re.sub(r'DASH_\d+.*\.mp4', 'DASH_audio.mp4', video_url)
            audio_url_from_video_2 = re.sub(r'DASH_\d+.*\.mp4', 'DASH_audio.m4a', video_url)

            # Combine unique URLs to try
            audio_urls_to_try = []
            seen_urls = set()
            for url in [audio_url_pattern_1, audio_url_pattern_2, audio_url_pattern_3, audio_url_pattern_4, audio_url_from_video_1, audio_url_from_video_2]:
                 # Check if URL is different from video_url and not already added
                 if url != video_url and url not in seen_urls:
                      audio_urls_to_try.append(url)
                      seen_urls.add(url)


            for audio_url in audio_urls_to_try:
                if not audio_url: continue # Skip if regex substitution failed etc.

                self.logger.spam(self.indent_2 + f"Attempting to download audio component from: {audio_url}")
                try:
                    # Use requests for better error handling
                    headers = {'User-Agent': 'SavedditDownloader/1.0'}
                    response = requests.get(audio_url, stream=True, headers=headers, timeout=20) # Shorter timeout for audio
                    response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                    # Check content type if possible and if it seems like audio
                    content_type = response.headers.get('content-type', '').lower()
                    if content_type and not ('audio' in content_type or 'video' in content_type or 'octet-stream' in content_type):
                        self.logger.warning(self.indent_2 + f"URL {audio_url} returned non-audio/video content-type: {content_type}. Skipping this URL.")
                        response.close() # Close the connection
                        continue # Skip to next URL

                    with open(audio_save_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=1024 * 8): # 8KB chunks
                            f.write(chunk)

                    # Check if the downloaded file exists and is reasonably sized (e.g., > 1KB)
                    if os.path.exists(audio_save_path) and os.path.getsize(audio_save_path) > 1024:
                        audio_downloaded = True
                        self.logger.spam(self.indent_2 + f"Successfully downloaded audio from {audio_url}.")
                        break # Stop trying URLs once successful
                    else:
                        self.logger.warning(self.indent_2 + f"Downloaded file from {audio_url} is too small or empty. Likely an error page or no audio track. Cleaning up.")
                        if os.path.exists(audio_save_path):
                            try: os.remove(audio_save_path)
                            except OSError: pass # Ignore removal error

                except requests.exceptions.HTTPError as http_err:
                     # Log 4xx/5xx errors specifically, common for non-existent audio tracks (403 Forbidden or 404 Not Found)
                     self.logger.spam(self.indent_2 + f"Failed to download audio from {audio_url}. Status: {http_err.response.status_code}")
                     # Clean up potential empty/error file
                     if os.path.exists(audio_save_path):
                         try: os.remove(audio_save_path)
                         except OSError: pass
                except requests.exceptions.RequestException as req_e:
                    # Log other connection errors
                    self.logger.warning(self.indent_2 + f"Connection or request error for audio URL {audio_url}. Error: {req_e}")
                    if os.path.exists(audio_save_path):
                         try: os.remove(audio_save_path)
                         except OSError: pass
                except Exception as e: # Catch other potential errors like file system issues
                    self.logger.error(self.indent_2 + f"An unexpected error occurred trying to download audio from {audio_url}")
                    self.print_formatted_error(e)
                    if os.path.exists(audio_save_path):
                        try: os.remove(audio_save_path)
                        except OSError: pass

            # --- Merging ---
            if audio_downloaded:
                self.logger.spam(self.indent_2 + "Merging video & audio components with ffmpeg")
                output_save_path = os.path.join(output_path, media_id + ".mp4")

                # Ensure paths are quoted for safety with spaces/special chars in filenames/paths
                quoted_video_path = f'"{video_save_path}"'
                quoted_audio_path = f'"{audio_save_path}"'
                quoted_output_path = f'"{output_save_path}"'

                # Simplified and robust ffmpeg command
                # -y: Overwrite output without asking
                # -loglevel error: Show only critical errors from ffmpeg
                # -c:v copy: Copy video stream without re-encoding (fast)
                # -c:a aac: Re-encode audio to AAC (widely compatible). Use 'copy' ONLY if you know the source is compatible (e.g., AAC).
                # -shortest: Finish encoding when the shortest input stream ends (useful if audio/video lengths differ slightly)
                ffmpeg_cmd = f'ffmpeg -loglevel error -i {quoted_video_path} -i {quoted_audio_path} -c:v copy -c:a aac -shortest {quoted_output_path} -y'
                self.logger.spam(self.indent_2 + f"Executing FFmpeg command: {ffmpeg_cmd}")

                try:
                    # Using os.system is simple but lacks good error capture.
                    # subprocess is generally preferred for more control.
                    # result = os.system(ffmpeg_cmd)

                    # Using subprocess to capture stderr
                    process = subprocess.run(ffmpeg_cmd, shell=True, capture_output=True, text=True, check=False) # check=False to handle non-zero exits manually
                    result = process.returncode
                    ffmpeg_stderr = process.stderr.strip()

                    # Check result code AND file existence/size
                    # Basic check: output file exists and is at least 80% of the video file size (accounts for audio overhead)
                    merge_successful = False
                    if result == 0 and os.path.exists(output_save_path) and os.path.getsize(output_save_path) > os.path.getsize(video_save_path) * 0.8 :
                        merge_successful = True
                        self.logger.spam(self.indent_2 + "Successfully merged with ffmpeg.")
                    else:
                        # Log error even if result code was 0 but file seems invalid
                        self.logger.error(self.indent_2 + f"FFmpeg merge command finished (code {result}) but output seems invalid or failed.")
                        if ffmpeg_stderr:
                             self.logger.error(self.indent_2 + "FFmpeg stderr:")
                             for line in ffmpeg_stderr.splitlines():
                                 self.logger.error(self.indent_2 + "  " + line)
                        else:
                             self.logger.error(self.indent_2 + "(No stderr captured from FFmpeg or command failed early)")

                    # Cleanup or fallback based on merge success
                    if merge_successful:
                        # Clean up temporary files AFTER successful merge
                        try:
                            if os.path.exists(video_save_path): os.remove(video_save_path)
                            if os.path.exists(audio_save_path): os.remove(audio_save_path)
                            self.logger.spam(self.indent_2 + "Cleaned up temporary video and audio files.")
                        except OSError as rm_err:
                            self.logger.warning(self.indent_2 + f"Could not remove temporary files after merge: {rm_err}")
                    else: # Merge failed
                         self.logger.error(self.indent_2 + "Using video without audio due to merge failure.")
                         # Fallback: Rename video-only file to the final name
                         final_path = os.path.join(output_path, media_id + ".mp4")
                         try:
                              # Ensure the target doesn't exist from a failed merge attempt
                              if os.path.exists(final_path): os.remove(final_path)
                              # Rename the original video file
                              os.rename(video_save_path, final_path)
                              self.logger.spam(self.indent_2 + f"Saved video (no audio) to {final_path}")
                         except OSError as ren_err:
                              self.logger.error(self.indent_2 + f"Could not rename video file after merge failure: {ren_err}")
                              # Important: If rename fails, the _video.mp4 file might be left behind.

                         # Clean up the audio file if it exists
                         if os.path.exists(audio_save_path):
                              try: os.remove(audio_save_path)
                              except OSError: pass # Ignore if removal fails

                except FileNotFoundError:
                    # Handle case where ffmpeg command is not found
                    self.logger.critical(self.indent_2 + "FFmpeg command not found. Please ensure FFmpeg is installed and in your system's PATH.")
                    # Fallback to video-only
                    final_path = os.path.join(output_path, media_id + ".mp4")
                    if os.path.exists(video_save_path) and video_save_path != final_path:
                         try:
                              if os.path.exists(final_path): os.remove(final_path)
                              os.rename(video_save_path, final_path)
                              self.logger.warning(self.indent_2 + f"Saved video only to {final_path} (FFmpeg not found).")
                         except OSError as ren_err: self.logger.error(f"Could not rename video file (FFmpeg not found): {ren_err}")
                    # Cleanup audio if downloaded
                    if os.path.exists(audio_save_path):
                         try: os.remove(audio_save_path)
                         except OSError: pass

                except Exception as ffmpeg_err:
                    self.logger.error(self.indent_2 + "An unexpected error occurred during FFmpeg execution.")
                    self.print_formatted_error(ffmpeg_err)
                    # Fallback logic if ffmpeg command itself crashes unexpectedly
                    final_path = os.path.join(output_path, media_id + ".mp4")
                    if os.path.exists(video_save_path):
                        try:
                            if os.path.exists(final_path): os.remove(final_path)
                            os.rename(video_save_path, final_path)
                            self.logger.warning(self.indent_2 + f"Saved video only to {final_path} due to FFmpeg error.")
                        except OSError as ren_err: self.logger.error(f"Could not rename video file after FFmpeg error: {ren_err}")
                    # Cleanup audio if downloaded
                    if os.path.exists(audio_save_path):
                        try: os.remove(audio_save_path)
                        except OSError: pass

            else: # No audio was downloaded
                self.logger.spam(self.indent_2 + "No audio component found or downloaded. Renaming video file.")
                final_path = os.path.join(output_path, media_id + ".mp4")
                try:
                    # Ensure final path doesn't exist before renaming, unless it's the same file
                    if os.path.exists(final_path) and os.path.abspath(final_path) != os.path.abspath(video_save_path):
                         os.remove(final_path)

                    # Rename only if the source exists and the name is different
                    if os.path.exists(video_save_path) and os.path.abspath(final_path) != os.path.abspath(video_save_path):
                        os.rename(video_save_path, final_path)
                        self.logger.spam(self.indent_2 + f"Saved video (no audio) to {final_path}")
                    elif os.path.exists(final_path) and os.path.abspath(final_path) == os.path.abspath(video_save_path):
                         # This case should ideally not happen if names are _video.mp4 vs .mp4
                         # but handles edge case where video_save_path was already the final path somehow
                         self.logger.spam(self.indent_2 + f"Video file already at final destination (no audio): {final_path}")
                    elif not os.path.exists(video_save_path):
                         self.logger.error(self.indent_2 + f"Source video file {video_save_path} missing, cannot save video-only file.")


                except OSError as ren_err:
                    self.logger.error(self.indent_2 + f"Failed to rename video-only file: {ren_err}")
                    # Ensure the original _video.mp4 isn't left orphaned if rename fails and it still exists
                    if not os.path.exists(final_path) and os.path.exists(video_save_path):
                        self.logger.warning(self.indent_2 + f"Original video component remains at {video_save_path}")

        else:
            # Handle cases where media or reddit_video data structure is missing/invalid after checking everything
            self.logger.warning(self.indent_2 + f"Could not find processable reddit_video data for submission {submission.id} ({submission.url}). Media structure might be unexpected.")
            # pprint(media) # Uncomment to inspect the media structure if debugging is needed

    # --- END UPDATED download_reddit_video ---


    def is_gfycat_link(self, url):
        try:
             return "gfycat.com" in urllib.parse.urlparse(url).netloc.lower()
        except Exception: return False

    def is_redgifs_link(self, url):
         try:
              # Redgifs often uses different subdomains like i.redgifs.com, www.redgifs.com, etc.
              return "redgifs.com" in urllib.parse.urlparse(url).netloc.lower()
         except Exception: return False

    def get_gfycat_embedded_video_url(self, url):
        # Deprecated?: Gfycat often redirects now, this might not work reliably.
        # Keeping it as a fallback mechanism.
        self.logger.spam(self.indent_2 + f"Attempting to scrape gfycat page for embedded video: {url}")
        try:
            headers = {'User-Agent': 'SavedditDownloader/1.0'}
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status() # Check for HTTP errors

            soup = BeautifulSoup(response.content, 'html.parser') # Use response.content for correct encoding handling

            # Look for video tags first (more reliable if present)
            video_tags = soup.find_all('video')
            for video in video_tags:
                 source_tags = video.find_all('source')
                 for source in source_tags:
                      src = source.get('src')
                      # Prioritize URLs containing 'giant' or 'thumbs' as before, but accept others
                      if src and src.endswith('.mp4'):
                          if "giant." in src or "zippy." in src: # Zippy is another common one
                               self.logger.spam(self.indent_2 + f"Found high-quality MP4 source: {src}")
                               return src
                          elif "thumbs." in src:
                               self.logger.spam(self.indent_2 + f"Found thumbs MP4 source: {src}")
                               return src # Return thumbs if giant isn't found
                          else:
                               # Found an MP4 source, maybe less preferred quality
                               self.logger.spam(self.indent_2 + f"Found other MP4 source: {src}")
                               return src # Return it as a last resort within <video> tags


            # Fallback: Look for JSON data within script tags (sometimes contains URLs)
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string: # Check if script tag has content
                    try:
                        # Look for JSON structures that might contain URLs
                        # This is highly specific to page structure and likely to break
                        if 'contentUrl' in script.string:
                             # Very basic search, might need regex for robustness
                             match = re.search(r'"contentUrl"\s*:\s*"([^"]+\.mp4)"', script.string)
                             if match:
                                  url_found = match.group(1)
                                  self.logger.spam(self.indent_2 + f"Found MP4 URL in script tag JSON: {url_found}")
                                  return url_found
                    except Exception:
                         continue # Ignore errors parsing script content

            self.logger.warning(self.indent_2 + f"Could not find a suitable .mp4 video source on page {url}")
            return "" # Return empty string if nothing found

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to connect to or scrape {url}")
            self.print_formatted_error(e)
            return ""
        except Exception as e:
            # Catch other errors like BeautifulSoup issues
            self.logger.error(self.indent_2 + f"An unexpected error occurred while scraping {url}")
            self.print_formatted_error(e)
            return ""


    def guess_extension(self, url, response_headers=None):
        # Guess extension based on URL first, then Content-Type header
        # 1. Try URL path extension
        try:
            path = urllib.parse.urlparse(url).path
            name, ext = os.path.splitext(path)
            if ext and len(ext) <= 5: # Basic check for valid extension format
                 return ext.lower()
        except Exception: pass # Ignore URL parsing errors

        # 2. Try Content-Type header if provided
        if response_headers and 'content-type' in response_headers:
            content_type = response_headers['content-type'].split(';')[0] # Ignore charset etc.
            guessed_ext = mimetypes.guess_extension(content_type)
            if guessed_ext:
                 return guessed_ext.lower()

        # 3. Fallback if needed (or return None/empty)
        # self.logger.warning(self.indent_2 + f"Could not determine file extension for {url}")
        return ".bin" # Or return None, or raise an error? Defaulting to .bin (binary data)


    def get_redirect_url(self, url):
        # Finds the *final* URL after following redirects.
        try:
            headers = {'User-Agent': 'SavedditDownloader/1.0'}
            # Use HEAD request first (faster, less data) if server supports it well for redirects
            # Fallback to GET if HEAD fails or doesn't redirect properly
            try:
                 response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
                 response.raise_for_status() # Check for client/server errors on final URL
                 return response.url
            except requests.exceptions.RequestException as head_err:
                 self.logger.spam(f"HEAD request failed for {url} ({head_err}), trying GET.")
                 response = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
                 response.raise_for_status()
                 return response.url

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to connect or resolve redirects for {url}")
            self.print_formatted_error(e)
            return None
        except Exception as e:
             self.logger.error(self.indent_2 + f"Unexpected error getting redirect URL for {url}")
             self.print_formatted_error(e)
             return None


    def download_gfycat_or_redgif(self, submission, output_dir):
        # Returns True on success, False on failure
        original_url = submission.url
        final_url = original_url

        # --- 1. Check for Redirects (Common for Gfycat -> Redgifs/Gifdeliverynetwork) ---
        self.logger.spam(self.indent_2 + f"Checking for redirects from: {original_url}")
        redirected_url = self.get_redirect_url(original_url)

        if redirected_url and redirected_url != original_url:
             self.logger.spam(self.indent_2 + f"Redirected to: {redirected_url}")
             final_url = redirected_url # Use the final URL for further checks
        elif not redirected_url:
             self.logger.warning(self.indent_2 + "Failed to resolve URL, attempting download with original URL.")
             # Continue with original_url, but success is less likely
        else:
            self.logger.spam(self.indent_2 + "No redirect detected.")


        # --- 2. Try PRAW's Preview Data First (Often reliable for Redgifs/Gfycat) ---
        # Look for reddit_video_preview (MP4 version) or standard image preview
        try:
            if hasattr(submission, 'preview') and submission.preview:
                preview = submission.preview
                # Prioritize reddit_video_preview if available
                if 'reddit_video_preview' in preview and preview['reddit_video_preview']:
                     video_preview = preview['reddit_video_preview']
                     fallback_url = video_preview.get('fallback_url')
                     if fallback_url and fallback_url.endswith('.mp4'):
                         self.logger.spam(self.indent_2 + "Found reddit_video_preview MP4 URL.")
                         # Use fallback_url directly
                         filename_base = os.path.basename(urllib.parse.urlparse(original_url).path) or submission.id
                         filename = f"{filename_base}.mp4"
                         save_path = os.path.join(output_dir, filename)
                         if self.download_direct_link(type('obj', (object,),{'url': fallback_url})(), save_path): # Create dummy object for download_direct_link
                             return True
                         else:
                             self.logger.warning(self.indent_2 + "Failed to download from reddit_video_preview URL.")
                             # Continue to other methods...
                # Fallback to standard image preview if no video preview
                elif 'images' in preview and preview['images']:
                    image_info = preview['images'][0]
                    source_url = image_info.get('source', {}).get('url')
                    # Resolutions might contain gif/mp4 versions too
                    resolutions = image_info.get('resolutions', [])
                    # Look for MP4 or GIF in resolutions if source isn't ideal
                    best_preview_url = source_url # Start with source
                    file_ext = None

                    # Check if source is useful (e.g., not a low-res jpg)
                    if source_url:
                        # Decode HTML entities like &
                        source_url = html.unescape(source_url)
                        # Guess extension
                        ext_guess = self.guess_extension(source_url)
                        if ext_guess in ['.gif', '.mp4']:
                            best_preview_url = source_url
                            file_ext = ext_guess
                        # If source is just an image, check resolutions for video/gif
                        elif ext_guess in ['.jpg', '.png', '.jpeg']:
                             # Look through resolutions for a better format
                             for res in resolutions:
                                  res_url = html.unescape(res.get('url'))
                                  res_ext = self.guess_extension(res_url)
                                  if res_ext == '.mp4':
                                       best_preview_url = res_url
                                       file_ext = '.mp4'
                                       break # Prefer MP4
                                  elif res_ext == '.gif' and file_ext != '.mp4':
                                       best_preview_url = res_url
                                       file_ext = '.gif' # Take GIF if MP4 not found yet

                    if best_preview_url and file_ext:
                        self.logger.spam(self.indent_2 + f"Found preview image/video source ({file_ext}): {best_preview_url}")
                        filename_base = os.path.basename(urllib.parse.urlparse(original_url).path) or submission.id
                        filename = f"{filename_base}{file_ext}"
                        save_path = os.path.join(output_dir, filename)
                        # Use download_direct_link again
                        if self.download_direct_link(type('obj', (object,),{'url': best_preview_url})(), save_path):
                            return True
                        else:
                            self.logger.warning(self.indent_2 + "Failed to download from preview images URL.")
                            # Continue...
                    else:
                         self.logger.spam(self.indent_2 + "No suitable MP4/GIF found in submission.preview data.")

        except Exception as e:
           self.logger.error(self.indent_2 + "Error processing submission.preview data.")
           self.print_formatted_error(e)
           # Continue to other methods...

        # --- 3. Try Scraping the Final URL (Gfycat/Redgifs Page) ---
        # This is less reliable due to site changes but acts as a fallback.
        # Primarily useful if the URL is gfycat.com or redgifs.com domain
        domain = urllib.parse.urlparse(final_url).netloc.lower()
        if "gfycat.com" in domain or "redgifs.com" in domain:
             self.logger.spam(self.indent_2 + f"Attempting to scrape page for video URL: {final_url}")
             embedded_video_url = self.get_gfycat_embedded_video_url(final_url) # Reusing the gfycat scraping logic

             if embedded_video_url:
                  self.logger.spam(self.indent_2 + f"Found embedded video URL via scraping: {embedded_video_url}")
                  # Determine filename
                  try:
                      filename = os.path.basename(urllib.parse.urlparse(embedded_video_url).path)
                      if not filename: # Fallback if path parsing fails
                           filename_base = os.path.basename(urllib.parse.urlparse(original_url).path) or submission.id
                           filename = f"{filename_base}.mp4" # Assume mp4
                  except Exception:
                       filename_base = os.path.basename(urllib.parse.urlparse(original_url).path) or submission.id
                       filename = f"{filename_base}.mp4"

                  save_path = os.path.join(output_dir, filename)
                  # Use download_direct_link
                  if self.download_direct_link(type('obj', (object,),{'url': embedded_video_url})(), save_path):
                       return True
                  else:
                       self.logger.warning(self.indent_2 + "Failed to download scraped embedded video URL.")
                       # Continue...
             else:
                 self.logger.warning(self.indent_2 + "Scraping did not yield a video URL.")

        # --- 4. Use youtube-dl as a Last Resort ---
        # If previous methods failed, maybe youtube-dl supports the URL directly.
        self.logger.spam(self.indent_2 + f"Trying youtube-dl as a fallback for: {final_url}")
        if self.download_youtube_video(final_url, output_dir):
             # Assume ytdl handled naming correctly based on its template
             self.logger.spam(self.indent_2 + "youtube-dl successfully downloaded the content.")
             return True
        else:
             self.logger.warning(self.indent_2 + "youtube-dl fallback failed.")


        # --- If all methods failed ---
        self.logger.error(self.indent_2 + f"All methods failed to download media for: {original_url}")
        return False


    def is_imgur_album(self, url):
        # Matches /a/albumId or /gallery/galleryId
        try:
            parsed_url = urllib.parse.urlparse(url.lower())
            if parsed_url.netloc.endswith('imgur.com'):
                 # Check path structure
                 return parsed_url.path.startswith('/a/') or parsed_url.path.startswith('/gallery/')
            return False
        except Exception:
            return False

    def get_imgur_album_images_count(self, album_id):
        # Returns count or 0 on error/empty
        if not self.IMGUR_CLIENT_ID:
             self.logger.error(self.indent_2 + "Imgur Client ID not available. Cannot get album info.")
             return 0

        request_url = f"https://api.imgur.com/3/album/{album_id}"
        headers = {"Authorization": f"Client-ID {self.IMGUR_CLIENT_ID}"}

        try:
            response = requests.get(request_url, headers=headers, timeout=15)
            response.raise_for_status() # Raise error for 4xx/5xx responses

            data = response.json()
            if data.get("success"):
                count = data.get("data", {}).get("images_count", 0)
                if count == 0:
                    self.logger.spam(self.indent_2 + f"Imgur album '{album_id}' reported 0 images.")
                return count
            else:
                error_msg = data.get("data", {}).get("error", "Unknown error")
                self.logger.error(self.indent_2 + f"Imgur API error getting album info for '{album_id}': {error_msg}")
                return 0

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to connect to Imgur API for album '{album_id}' count.")
            self.print_formatted_error(e)
            return 0
        except json.JSONDecodeError as e:
             self.logger.error(self.indent_2 + f"Failed to parse Imgur API response for album '{album_id}' count.")
             self.print_formatted_error(e)
             return 0
        except Exception as e:
             self.logger.error(self.indent_2 + f"Unexpected error getting Imgur album '{album_id}' count.")
             self.print_formatted_error(e)
             return 0


    def get_imgur_image_meta(self, image_id):
        # Returns image metadata dict or None on error
        if not self.IMGUR_CLIENT_ID:
             self.logger.error(self.indent_2 + "Imgur Client ID not available. Cannot get image meta.")
             return None

        request_url = f"https://api.imgur.com/3/image/{image_id}"
        headers = {"Authorization": f"Client-ID {self.IMGUR_CLIENT_ID}"}

        try:
            response = requests.get(request_url, headers=headers, timeout=15)
            response.raise_for_status()

            data = response.json()
            if data.get("success"):
                return data.get("data") # Return the 'data' dictionary
            else:
                error_msg = data.get("data", {}).get("error", "Unknown error")
                self.logger.error(self.indent_2 + f"Imgur API error getting image meta for '{image_id}': {error_msg}")
                return None

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to connect to Imgur API for image '{image_id}' meta.")
            self.print_formatted_error(e)
            return None
        except json.JSONDecodeError as e:
            self.logger.error(self.indent_2 + f"Failed to parse Imgur API response for image '{image_id}' meta.")
            self.print_formatted_error(e)
            return None
        except Exception as e:
            self.logger.error(self.indent_2 + f"Unexpected error getting Imgur image '{image_id}' meta.")
            self.print_formatted_error(e)
            return None

    def download_imgur_album(self, submission, output_dir):
        # Returns True if download process started (even if individual items fail), False on major setup error
        if not self.IMGUR_CLIENT_ID:
             self.logger.error(self.indent_1 + "Cannot download Imgur album: Client ID missing.")
             return False

        album_id = None
        parsed_url = urllib.parse.urlparse(submission.url)
        path_parts = parsed_url.path.strip('/').split('/')

        # Expected path: /a/albumId or /gallery/galleryId
        if len(path_parts) == 2 and path_parts[0] in ('a', 'gallery'):
             album_id = path_parts[1]

        if not album_id:
            self.logger.error(self.indent_2 + f"Could not extract Imgur album ID from URL: {submission.url}")
            return False

        self.logger.spam(self.indent_2 + f"Processing Imgur album ID: {album_id}")

        # Get album info using the API (includes image list)
        request_url = f"https://api.imgur.com/3/album/{album_id}"
        headers = {"Authorization": f"Client-ID {self.IMGUR_CLIENT_ID}"}

        try:
            response = requests.get(request_url, headers=headers, timeout=20)
            response.raise_for_status()
            album_data = response.json()

            if not album_data.get("success"):
                error_msg = album_data.get("data", {}).get("error", "Unknown API error")
                self.logger.error(self.indent_2 + f"Imgur API error fetching album '{album_id}': {error_msg}")
                return False

            images = album_data.get("data", {}).get("images")
            if not images:
                self.logger.warning(self.indent_2 + f"Imgur album '{album_id}' contains no images or data is invalid.")
                return True # No images to download, consider it "success"

            images_count = len(images)
            self.logger.spam(self.indent_2 + f"Imgur album has {images_count} image(s)/video(s)")

            download_count = 0
            for i, image_meta in tqdm(enumerate(images), total=images_count, bar_format='%s%s{l_bar}{bar:20}{r_bar}%s' % (self.indent_2, Fore.WHITE + Fore.LIGHTBLACK_EX, Fore.RESET), leave=False):
                 image_url = image_meta.get("link")
                 image_id = image_meta.get("id")
                 image_type = image_meta.get("type", "image/jpeg").split('/')[1] # Default to jpeg if type missing

                 if not image_url or not image_id:
                      self.logger.warning(self.indent_2 + f"Skipping album item {i+1}: Missing link or ID.")
                      continue

                 # Construct filename: index_imageId.extension
                 filename = f"{str(i).zfill(3)}_{image_id}.{image_type}"
                 save_path = os.path.join(output_dir, filename)

                 # Use download_direct_link method
                 if self.download_direct_link(type('obj', (object,),{'url': image_url})(), save_path):
                     download_count += 1
                 # download_direct_link already logs errors on failure

            self.logger.spam(self.indent_2 + f"Finished processing Imgur album. Downloaded {download_count}/{images_count} items.")
            return True # Return True even if some downloads failed, as the process ran

        except requests.exceptions.RequestException as e:
            self.logger.error(self.indent_2 + f"Failed to connect to Imgur API for album '{album_id}'.")
            self.print_formatted_error(e)
            return False
        except json.JSONDecodeError as e:
            self.logger.error(self.indent_2 + f"Failed to parse Imgur API response for album '{album_id}'.")
            self.print_formatted_error(e)
            return False
        except Exception as e:
            self.logger.error(self.indent_2 + f"Unexpected error processing Imgur album '{album_id}'.")
            self.print_formatted_error(e)
            return False


    def is_imgur_image(self, url):
        # Checks if it's an imgur.com domain but NOT an album or gallery link
        try:
            parsed_url = urllib.parse.urlparse(url.lower())
            if parsed_url.netloc.endswith('imgur.com'):
                 # Check path structure - reject album/gallery paths
                 if not (parsed_url.path.startswith('/a/') or parsed_url.path.startswith('/gallery/')):
                      # Assume it's an image/video link if not album/gallery
                      return True
            return False
        except Exception:
            return False


    def download_imgur_image(self, submission, output_dir):
         # Handles single Imgur images/videos (not albums) identified by is_imgur_image
         # Returns True on success, False on failure
        if not self.IMGUR_CLIENT_ID:
             self.logger.error(self.indent_1 + "Cannot download Imgur image/video: Client ID missing.")
             return False

        # Extract image ID from URL (more robustly)
        # e.g., https://imgur.com/gallery/abcd -> abcd
        # e.g., https://imgur.com/abcd -> abcd
        # e.g., https://i.imgur.com/abcd.jpg -> abcd
        image_id = None
        parsed_url = urllib.parse.urlparse(submission.url)
        path = parsed_url.path.strip('/')

        if path:
            # Handle direct image links (e.g., /abcd.jpg) or plain IDs (e.g., /abcd)
            potential_id = os.path.splitext(path.split('/')[-1])[0] # Get last part of path, remove extension
            # Basic sanity check for typical Imgur ID format (alphanumeric, usually 5 or 7 chars)
            if re.match(r'^[a-zA-Z0-9]{5,}$', potential_id):
                 image_id = potential_id

        if not image_id:
            self.logger.error(self.indent_2 + f"Could not extract valid Imgur image ID from URL: {submission.url}")
            return False

        self.logger.spam(self.indent_2 + f"Processing Imgur image/video ID: {image_id}")

        # Get image metadata using API
        data = self.get_imgur_image_meta(image_id)

        if not data:
            # get_imgur_image_meta already logged the error
            return False

        # Extract relevant info
        url = data.get("link")
        content_type = data.get("type", "") # e.g., "image/jpeg", "video/mp4"
        is_video = data.get("is_album") is False and data.get("has_sound", False) # Check if it's likely a video

        if not url:
             self.logger.error(self.indent_2 + f"Imgur API metadata for '{image_id}' missing 'link'.")
             return False

        # Determine extension
        file_ext = None
        if content_type:
            file_ext = mimetypes.guess_extension(content_type.split(';')[0])
        if not file_ext: # Fallback guess from URL if type missing/unknown
             file_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1]
        if not file_ext: # Final fallback
             file_ext = ".jpg" if "image" in content_type else ".mp4" if "video" in content_type else ".bin"

        # Determine filename
        filename = f"{image_id}{file_ext}"
        save_path = os.path.join(output_dir, filename)

        # Log type
        if "video" in content_type or is_video:
            self.logger.spam(self.indent_2 + "This is an imgur link to a video file.")
        elif "image" in content_type:
             self.logger.spam(self.indent_2 + "This is an imgur link to an image file.")
        else:
             self.logger.spam(self.indent_2 + "This is an imgur link (unknown content type).")


        # Download using the direct link method
        if self.download_direct_link(type('obj', (object,),{'url': url})(), save_path):
            return True
        else:
            # Error already logged by download_direct_link
            return False


    def download_comments(self, submission, output_dir, comment_limit):
        # Returns True if comments saved (or none exist), False on error
        comments_list = []
        comments_json_path = os.path.join(output_dir, 'comments.json')

        try:
            # Fetch comments - Use limit=None for all, or a number for top-level limit + replies below
            # PRAW's limit parameter applies to the number of MoreComments objects replaced.
            # For true top-level comment limit, fetch and slice.
            self.logger.spam(self.indent_2 + "Fetching comments...")
            submission.comments.replace_more(limit=None) # Replace *all* MoreComments objects first

            # Get the full list after replacing 'more' comments
            all_comments = submission.comments.list()

            if not all_comments:
                self.logger.spam(self.indent_2 + "No comments found for this submission.")
                # Create an empty comments file for consistency? Optional.
                # with open(comments_json_path, 'w', encoding='utf-8') as file:
                #     json.dump([], file, indent=2)
                return True # No comments is not an error

            # Apply limit if specified (after fetching all)
            comments_to_process = all_comments
            if comment_limit is not None and comment_limit >= 0:
                # This limits the total number of comments processed, not just top-level
                self.logger.spam(self.indent_2 + f"Limiting processed comments to {comment_limit}.")
                comments_to_process = all_comments[:comment_limit]


            self.logger.spam(self.indent_2 + f"Processing {len(comments_to_process)} comments...")
            for comment in tqdm(comments_to_process, total=len(comments_to_process), bar_format='%s%s{l_bar}{bar:20}{r_bar}%s' % (self.indent_2, Fore.WHITE + Fore.LIGHTBLACK_EX, Fore.RESET), leave=False):
                # Check if it's a valid Comment object (not MoreComments that failed replacement)
                if not isinstance(comment, praw.models.Comment):
                     self.logger.warning(self.indent_2 + f"Skipping non-comment object in list: {type(comment)}")
                     continue

                comment_dict = {}
                try:
                    # Access attributes safely using getattr
                    comment_dict["author"] = getattr(comment.author, 'name', None) # Handle deleted author
                    comment_dict["body"] = getattr(comment, 'body', "")
                    comment_dict["created_utc"] = int(getattr(comment, 'created_utc', 0))
                    comment_dict["distinguished"] = getattr(comment, 'distinguished', None)
                    # comment_dict["downs"] = getattr(comment, 'downs', 0) # 'downs' is deprecated/always 0
                    comment_dict["edited"] = getattr(comment, 'edited', False)
                    comment_dict["id"] = getattr(comment, 'id', None)
                    comment_dict["is_submitter"] = getattr(comment, 'is_submitter', False)
                    comment_dict["link_id"] = getattr(comment, 'link_id', None) # Submission ID
                    comment_dict["parent_id"] = getattr(comment, 'parent_id', None) # Comment or Submission ID
                    comment_dict["permalink"] = getattr(comment, 'permalink', None)
                    comment_dict["score"] = getattr(comment, 'score', 0)
                    comment_dict["stickied"] = getattr(comment, 'stickied', False)
                    comment_dict["subreddit_name_prefixed"] = getattr(getattr(comment, 'subreddit', None), 'display_name', None)
                    comment_dict["subreddit_id"] = getattr(comment, 'subreddit_id', None)
                    comment_dict["total_awards_received"] = getattr(comment, 'total_awards_received', 0)
                    # comment_dict["ups"] = getattr(comment, 'ups', 0) # 'ups' is deprecated/approximated by score

                    comments_list.append(comment_dict)

                except Exception as comment_err:
                    # Log error for specific comment but continue with others
                    comment_id = getattr(comment, 'id', 'UNKNOWN_ID')
                    self.logger.error(self.indent_2 + f"Error processing comment ID: {comment_id}")
                    self.print_formatted_error(comment_err)
                    # Optionally add a placeholder or skip the comment entirely
                    # comments_list.append({"id": comment_id, "error": str(comment_err)})


            # Write the collected comments to JSON file
            with open(comments_json_path, 'w', encoding='utf-8') as file:
                json.dump(comments_list, file, indent=2, ensure_ascii=False) # ensure_ascii=False for proper unicode

            self.logger.spam(self.indent_2 + f"Successfully saved {len(comments_list)} comments to {comments_json_path}")
            return True

        except praw.exceptions.PRAWException as praw_e:
             self.logger.error(self.indent_2 + "PRAW error fetching or processing comments.")
             self.print_formatted_error(praw_e)
             return False
        except Exception as e:
            self.logger.error(self.indent_2 + "An unexpected error occurred saving comments.")
            self.print_formatted_error(e)
            # Clean up potentially incomplete JSON file?
            # if os.path.exists(comments_json_path):
            #     try: os.remove(comments_json_path)
            #     except OSError: pass
            return False


    def is_self_post(self, submission):
        # Check the is_self attribute
        return getattr(submission, 'is_self', False)

    def download_submission_meta(self, submission, submission_dir):
        # Returns True on success, False on error
        submission_dict = {}
        meta_json_path = os.path.join(submission_dir, "submission.json")

        try:
            # Safely access attributes using getattr
            submission_dict["author"] = getattr(submission.author, 'name', None) # Handle deleted author
            submission_dict["created_utc"] = int(getattr(submission, 'created_utc', 0))
            submission_dict["distinguished"] = getattr(submission, 'distinguished', None)
            # submission_dict["downs"] = getattr(submission, 'downs', 0) # Deprecated
            submission_dict["edited"] = getattr(submission, 'edited', False)
            submission_dict["id"] = getattr(submission, 'id', None)
            submission_dict["is_original_content"] = getattr(submission, 'is_original_content', False)
            submission_dict["is_self"] = getattr(submission, 'is_self', False)
            submission_dict["is_video"] = getattr(submission, 'is_video', False)
            submission_dict["link_flair_text"] = getattr(submission, 'link_flair_text', None)
            submission_dict["locked"] = getattr(submission, 'locked', False)
            submission_dict["media"] = getattr(submission, 'media', None) # Include media metadata if present
            submission_dict["media_embed"] = getattr(submission, 'media_embed', {}) # Include embed info
            submission_dict["num_comments"] = getattr(submission, 'num_comments', 0)
            submission_dict["num_crossposts"] = getattr(submission, 'num_crossposts', 0)
            submission_dict["over_18"] = getattr(submission, 'over_18', False) # NSFW status
            submission_dict["permalink"] = getattr(submission, 'permalink', None)
            submission_dict["score"] = getattr(submission, 'score', 0)
            submission_dict["selftext"] = getattr(submission, 'selftext', "") # Body text for self-posts
            submission_dict["selftext_html"] = getattr(submission, 'selftext_html', None) # HTML version of body
            # submission_dict["send_replies"] = getattr(submission, 'send_replies', True) # Less relevant for archive
            submission_dict["spoiler"] = getattr(submission, 'spoiler', False)
            submission_dict["stickied"] = getattr(submission, 'stickied', False)
            submission_dict["subreddit_name_prefixed"] = getattr(getattr(submission, 'subreddit', None), 'display_name', None)
            submission_dict["subreddit_id"] = getattr(submission, 'subreddit_id', None)
            submission_dict["subreddit_subscribers"] = getattr(getattr(submission, 'subreddit', None), 'subscribers', 0)
            # submission_dict["subreddit_type"] = getattr(submission, 'subreddit_type', None) # PRAW might handle this via subreddit object
            submission_dict["title"] = getattr(submission, 'title', "")
            submission_dict["total_awards_received"] = getattr(submission, 'total_awards_received', 0)
            # submission_dict["ups"] = getattr(submission, 'ups', 0) # Deprecated
            submission_dict["upvote_ratio"] = getattr(submission, 'upvote_ratio', 0.0)
            submission_dict["url"] = getattr(submission, 'url', None) # The link the submission points to

            # Add gallery data if present (useful for context even if images downloaded separately)
            if hasattr(submission, 'gallery_data'):
                 submission_dict['gallery_data'] = submission.gallery_data
            if hasattr(submission, 'media_metadata'):
                 submission_dict['media_metadata'] = submission.media_metadata


            # Write to file
            with open(meta_json_path, 'w', encoding='utf-8') as file:
                # Use default=str for any objects json can't serialize directly (like datetime if it sneakily appears)
                json.dump(submission_dict, file, indent=2, ensure_ascii=False, default=str)

            return True

        except Exception as e:
            self.logger.error(self.indent_1 + "An unexpected error occurred saving submission metadata.")
            self.print_formatted_error(e)
            # Clean up potentially incomplete file?
            # if os.path.exists(meta_json_path):
            #     try: os.remove(meta_json_path)
            #     except OSError: pass
            return False


# Example Usage (requires setting up PRAW, logger, config etc.)
# if __name__ == '__main__':
#     # --- Setup Logger ---
#     # verboselogs.install() # Installs SUCCESS, NOTICE, SPAM, VERBOSE levels
#     # logger = logging.getLogger(__name__)
#     # formatter = coloredlogs.ColoredFormatter('%(asctime)s %(levelname)s %(message)s')
#     # handler = logging.StreamHandler()
#     # handler.setFormatter(formatter)
#     # logger.addHandler(handler)
#     # logger.setLevel('SPAM') # Set desired logging level (e.g., SPAM for max verbosity)
#     # logging.getLogger("prawcore").setLevel(logging.WARNING) # Quieten PRAW internal logs
#     # logging.getLogger("urllib3").setLevel(logging.WARNING)
#     # logging.getLogger("requests").setLevel(logging.WARNING)
#     # logging.getLogger("youtube_dl").setLevel(logging.WARNING)
#
#     # --- PRAW Setup ---
#     # reddit = praw.Reddit(
#     #     client_id="YOUR_CLIENT_ID",
#     #     client_secret="YOUR_CLIENT_SECRET",
#     #     user_agent="SavedditDownloader/1.0 by YourUsername",
#     #     # Optional: username/password for user context (needed for saved posts)
#     #     # username="YOUR_REDDIT_USERNAME",
#     #     # password="YOUR_REDDIT_PASSWORD",
#     # )
#
#     # --- Configuration ---
#     # config = {
#     #     "imgur_client_id": "YOUR_IMGUR_CLIENT_ID" # Optional, needed for Imgur API
#     # }
#     # output_directory = "reddit_downloads"
#     # subreddit_name = "oddlysatisfying"
#     # limit = 10 # Number of posts to fetch
#     # sort_order = "hot" # Or 'new', 'top', 'controversial'
#     # skip_videos = False
#     # skip_meta = False
#     # skip_comments = False
#     # comment_limit = 20 # Or None for all comments
#
#     # --- Fetch Submissions ---
#     # logger.success(f"Fetching {limit} submissions from r/{subreddit_name} (sorting by {sort_order})")
#     # subreddit = reddit.subreddit(subreddit_name)
#     # submissions = []
#     # try:
#     #     if sort_order == "hot":
#     #         submissions = list(subreddit.hot(limit=limit))
#     #     elif sort_order == "new":
#     #          submissions = list(subreddit.new(limit=limit))
#     #     elif sort_order == "top":
#     #          submissions = list(subreddit.top(limit=limit, time_filter="all")) # Add time_filter if needed
#     #     # Add other sort orders if needed
#     #     logger.success(f"Fetched {len(submissions)} submissions.")
#     # except Exception as e:
#     #      logger.critical(f"Failed to fetch submissions: {e}")
#     #      exit(1)
#
#     # --- Process Submissions ---
#     # if not os.path.exists(output_directory):
#     #     os.makedirs(output_directory)
#
#     # for i, submission in enumerate(submissions):
#     #      logger.info(f"--- Processing Submission {i+1}/{len(submissions)} (ID: {submission.id}) ---")
#     #      downloader = SubmissionDownloader(
#     #          submission=submission,
#     #          submission_index=i + 1,
#     #          logger=logger,
#     #          output_dir=output_directory,
#     #          skip_videos=skip_videos,
#     #          skip_meta=skip_meta,
#     #          skip_comments=skip_comments,
#     #          comment_limit=comment_limit,
#     #          config=config
#     #      )
#     #      # The __init__ method handles the download logic
#
#     # logger.success("Finished processing all fetched submissions.")