import obsws_python as obs
from obsws_python import error as obserror
from random import choice
from playsound import playsound
import ctypes as ct
from pathlib import Path
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from send2trash import send2trash
import win32gui, win32process, time, threading, psutil, shutil, logging, re, os, json, subprocess, websocket

class MegaScript:

    def __init__(self):
        self.evt = None
        self.req = None

        self.text_update_delay = 2
        self.emote_gen = self.get_emote()

        # define settings
        # TODO: make these configurable with a gui application at some point, would be cool
        self.EMOTICONS = [
            ":3",
            ":)",
            ":D",
            ":P",
            ":>",
            "xd",
            "uwu",
            "owo",
            ":O",
            ":3c",
            "3:",
            "c:"
        ]
        self.BANNED_STRINGS = [
            "windows default lock screen",
            "windows.ui.core.corewindow",
            "lockapp.exe",
            "xamlexplorerhostislandwindow",
            "explorer.exe",
            "mozilla firefox",
            "mozillawindowclass",
            "firefox.exe",
            "obs",
            "steam",
            "onecommander",
            "premiere",
            "photoshop",
            "terminal",
            "cmd",
            "visual studio",
            "notepad",
            "github",
            "mpv",
            "windows input experience",
            "program manager",
            "sharex"
        ]
        self.SPECIAL_WINDOW_SCENES = {
            "discord": "Discord Capture",
            "destiny": "Desktop Capture"
        }
        self.SPECIAL_WINDOWS_INPUTS = {
            "discord": "Discord Window Capture",
            "destiny": None
        }
        self.WINDOW_PROFILES = {
            "300FPS": ["valorant", "cs2", "siege"],
            "current": ["discord"]
        }
        self.DEFAULT_PROFILE = "120FPS"
        self.SCENE_AFK_NAME = "Alt Tabbed"
        self.SCENE_GAME_NAME = "Game Capture"
        self.GAME_CAPTURE_NAME = "Capture 0"
        self.SCENE_DISCORD_NAME = "Discord Capture"
        self.SCENE_DESKTOP_NAME = "Desktop Capture"

        self.user32 = ct.windll.user32
        # set dpi awareness to per-monitor mode: 
        # this fixes weird output from windows's DPI scaling
        ct.windll.shcore.SetProcessDpiAwareness(2)

        self.connect_attempts_interval = 5

        self.switcher_thread = None
        self.switcher_poll_interval = 1
        self.switcher_active = True

        self.change_tabbed_text_thread = None
        self.change_tabbed_text_poll_interval = 2

        self.buffer_timeout = 300
        self.stop_buffer_timeout = 60
        self.afk_timer = 0

        # solution for getting script running dir stolen from here:
        # https://stackoverflow.com/a/9350788
        self.script_path = Path(os.path.dirname(os.path.realpath(__file__)))

        self.modified_times = {}

        self.connected = False
        self.running = True

        logging.basicConfig(
            filename=Path.joinpath(self.script_path, "megascript.log"),
            filemode="a",
            format='%(asctime)s %(levelname)s %(module)s - %(funcName)s: %(message)s',
            datefmt='%Y-%m-%d %I:%M:%S %p',
            level=logging.WARNING
        )
        # disable base obsws_python logging
        logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
        self.logger = logging.getLogger("obs-megascript")
        self.logger_last_msg = ""

        self.commands_path = Path.joinpath(self.script_path, "commands.json")
        self.commands_observer = None
        self.commands_event = None

        self.instant_replay_requested = False

        self.SFX_RECORD_START = str(Path.joinpath(self.script_path, "assets", "recordingstartbeep.mp3"))
        self.SFX_RECORD_END = str(Path.joinpath(self.script_path, "assets", "recordingendbeep.mp3"))
        self.SFX_RECORD_ERROR = str(Path.joinpath(self.script_path, "assets", "error.mp3"))
        self.SFX_COMMAND_RECEIVED = str(Path.joinpath(self.script_path, "assets", "commandreceived.mp3"))

        self.log_info_norepeat("Megascript initialized! Connecting to OBS...")
        self.reset_commands_json()
        self.establish_connection()
        self.profile_switcher(self.DEFAULT_PROFILE)
        self.manage_resolution()
        self.log_info_norepeat("Connected to OBS!")

    def reset_commands_json(self):
        try:
            commands_data = None
            with open(self.commands_path, "r") as f:
                commands_data = json.load(f)
            commands_data["toggleSwitcher"] = False
            commands_data["instantReplay"] = False
            with open(self.commands_path, "w") as f:
                json.dump(commands_data, f)
        except Exception as error:
            self.logger.exception("Error while resetting commands.json!")

    def init_commands_observer(self):
        # create event handler
        class CommandsEvent(FileSystemEventHandler):
            def __init__(self):
                super().__init__()
            
            @staticmethod # this needs to be here so that self references the superclass and not CommandsEvent
            def on_any_event(event):

                # this try except block prevents duplicate events from occurring
                # stolen from here: https://stackoverflow.com/a/79415551
                try:
                    t = os.path.getmtime(event.src_path)
                    if event.src_path in self.modified_times and t == self.modified_times[event.src_path]:
                        # duplicate event
                        return
                    self.modified_times[event.src_path] = t
                except FileNotFoundError:
                    # file got deleted after event was triggered
                    try:
                        del self.modified_times[event.src_path]
                    except KeyError:
                        pass
                # continue processing event

                if event.is_directory:
                    return None

                elif event.event_type == 'modified':
                    if "commands.json" in event.src_path:
                        commands_data = None

                        try:
                            with open(self.commands_path, "r") as f:
                                commands_data = json.load(f)
                                if commands_data["toggleSwitcher"]:
                                    playsound(self.SFX_COMMAND_RECEIVED)
                                    self.switcher_active = not(self.switcher_active)
                                    self.log_info_norepeat(f"Toggling switcher to {self.switcher_active}.")
                                    commands_data["toggleSwitcher"] = False
                                
                                if commands_data["instantReplay"]:
                                    playsound(self.SFX_COMMAND_RECEIVED)
                                    self.log_info_norepeat("Attempting to initiate instant replay...")
                                    self.instant_replay()
                                    commands_data["instantReplay"] = False
                                
                            with open(self.commands_path, "w") as f:
                                json.dump(commands_data, f)
                        except Exception as error:
                            self.logger.exception("Error while processing command event!")

        # start observer thread
        self.commands_event = CommandsEvent()
        self.commands_observer = Observer()
        self.commands_observer.schedule(
            event_handler = self.commands_event, 
            path = self.script_path,
            recursive = False
        )
        self.commands_observer.start()
        

    def log_info_norepeat(self, msg, error=None):
        if not msg == self.logger_last_msg:
            if error:
                self.logger.info(msg, exc_info=error)
            else:
                self.logger.info(msg)
        self.logger_last_msg = msg

    def establish_connection(self):
        while not self.connected:
            try:
                self.req = obs.ReqClient()
                self.evt = obs.EventClient()

                # run some test funcs to make sure we actually have a connection to obs
                self.req.get_version()
                self.req.get_current_program_scene()
                self.req.get_stats()

                # at this point if we haven't thrown an error we're probably chill
                # set up the event callbacks and set connected to true
                self.evt.callback.register(self.on_replay_buffer_saved)
                #self.evt.callback.register(self.on_replay_buffer_state_changed)
                self.evt.callback.register(self.on_record_state_changed)
                self.connected = True
            except Exception as error:
                self.log_info_norepeat(f"Could not establish connection to OBS!", error=error)
                time.sleep(self.connect_attempts_interval)

    def handle_error(self, error):
        if isinstance(error, (
            obserror.OBSSDKTimeoutError, 
            json.JSONDecodeError, 
            ConnectionAbortedError,
            ConnectionResetError,
            ConnectionRefusedError,
            BrokenPipeError,
            websocket.WebSocketConnectionClosedException
        )):
            self.handle_connection_lost(error)
        else:
            self.logger.error(error, exc_info=error)

    def handle_connection_lost(self, error):
        if self.connected: # only trigger this once so we don't have multiple instances of establish_connection() running
            self.connected = False
            self.running = False

            # kill all other threads

            ### AI WRITTEN EXPLANATION FOR THIS CODE
            # The issue arises because handle_connection_lost is called from within one of the threads (e.g., change_tabbed_text), 
            # and it attempts to join the same thread that's executing the method, which is not allowed.

            # To fix this, I've added checks to prevent joining the current thread by comparing the thread's ident with threading.current_thread().ident.

            # This ensures that when a thread calls handle_connection_lost, it won't try to join itself. 
            # The threads will exit their loops naturally when self.running is set to False, and new threads will be started after reconnection.
            if self.change_tabbed_text_thread and self.change_tabbed_text_thread.ident != threading.current_thread().ident:
                self.change_tabbed_text_thread.join(timeout=5)
                self.change_tabbed_text_thread = None
            if self.switcher_thread and self.switcher_thread.ident != threading.current_thread().ident:
                self.switcher_thread.join(timeout=5)
                self.switcher_thread = None

            if self.commands_observer:
                self.commands_observer.stop()

            if self.commands_observer and self.commands_observer.ident != threading.current_thread().ident:
                self.commands_observer.join(timeout=5)
                self.commands_observer = None
            
            self.logger.warning("OBS connection failed, reconnecting...", exc_info=error)

            self.establish_connection()
            self.reset_commands_json()
            self.profile_switcher(self.DEFAULT_PROFILE)
            self.manage_resolution()
            self.instant_replay_requested = False
            
            # restart threads now that we're back online
            self.running = True
            if self.change_tabbed_text_thread is None:
                self.change_tabbed_text_thread = threading.Thread(target=self.change_tabbed_text)
                self.change_tabbed_text_thread.start()
            if self.switcher_thread is None:
                self.switcher_thread = threading.Thread(target=self.switcher)
                self.switcher_thread.start()
            self.init_commands_observer()

            self.log_info_norepeat(f"Reconnected to OBS!")

    def on_record_state_changed(self, data):
        saved_recording_data = None
        output_state = data.output_state
        if data.output_path is not None:
            saved_recording_data = Path(data.output_path)
        if output_state == "OBS_WEBSOCKET_OUTPUT_STOPPING":
            playsound(self.SFX_RECORD_START)
        elif output_state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            self.handle_saved_file(saved_recording_data)

    def on_replay_buffer_saved(self, data):
        playsound(self.SFX_RECORD_START)
        saved_replay = Path(data.saved_replay_path)
        self.handle_saved_file(saved_replay)

    def check_names_against_dir(self, names, directory):
        # this func stolen from here: https://stackoverflow.com/a/5320179
        def findWholeWord(w):
            return re.compile(r'\b({0})\b'.format(w), flags=re.IGNORECASE).search

        valid_dir = None
        winning_name = None
        search_root = Path(directory)

        for root, dirs, files in os.walk(search_root):
            for subdir in dirs:
                for name in names:
                    if findWholeWord(subdir)(name):
                        valid_dir = Path(root) / subdir
                        winning_name = name
                        break
                if valid_dir is not None:
                    break
            if valid_dir is not None:
                break

        # we still haven't found a dir if this triggers
        # iterate over dirs again, but this time match using in keyword
        if valid_dir is None:
            for root, dirs, files in os.walk(search_root):
                for subdir in dirs:
                    for name in names:
                        if subdir.lower() in name.lower():
                            valid_dir = Path(root) / subdir
                            winning_name = name
                            break
                    if valid_dir is not None:
                        break
                if valid_dir is not None:
                    break

        return valid_dir, winning_name
    
    def handle_saved_file(self, filepath):
        recording_dir = filepath.parents[0]
        moved = False
        valid_windows = self.get_valid_windows()

        if self.instant_replay_requested:
            try:
                self.instant_replay_requested = False
                mpv_args = [
                    "mpv",
                    "--pause",
                    filepath
                ]
                playsound(self.SFX_RECORD_END)
                subprocess.run(mpv_args)
                send2trash(filepath)
                self.log_info_norepeat(f"Sent '{filepath}' to trash after user exited instant replay successfully!")
            except Exception as error:
                self.logger.exception(f"Error while trying to play back instant replay '{filepath}'!")
        else:
            try:
                if valid_windows:
                    # check if we have focused windows
                    focused_windows = [window for window in valid_windows.values() if window.get("focused")]
                    correct_dir = None
                    if focused_windows:
                        # no need to sort through these further, should only have 1 focused window
                        # send that off to be checked against recording dirs5
                        focused_names = [window.get("obs_window_str") for window in focused_windows]
                        dir, name = self.check_names_against_dir(focused_names, recording_dir)
                        correct_dir = dir
                    else:
                        # no windows are focused, but we have valid windows still
                        # go through all nonspecial window names first and try to match them against a directory
                        nonspecial_names = [window.get("obs_window_str") for window in valid_windows.values() if not window.get("special_scene")]
                        special_names = [window.get("obs_window_str") for window in valid_windows.values() if window.get("special_scene")]
                        dir_nonspecial, name_nonspecial = self.check_names_against_dir(nonspecial_names, recording_dir)
                        dir_special, name_special = self.check_names_against_dir(special_names, recording_dir)
                        if dir_nonspecial:
                            correct_dir = dir_nonspecial
                        else:
                            # no nonspecial window names were valid
                            # set correct dir to be the special dir returned
                            correct_dir = dir_special
                            # if this is None that's fine because we check that next
                            # reason things are done in this order is because we prioritize nonspecial apps (i.e games) first
                    
                    # make our own dir if none is found
                    # use window name from first entry in fullscreen windows
                    if correct_dir is None:
                        first_window_name = next(iter(valid_windows.keys()))
                        correct_dir = Path.joinpath(recording_dir, first_window_name)
                        correct_dir.mkdir()

                    shutil.move(filepath, correct_dir)
                    moved = True
                else:
                    self.logger.warning(f"Could not move file '{filepath}'. No application was detected as valid.")

            except Exception as error:
                playsound(self.SFX_RECORD_ERROR)
                self.logger.exception(error)
                if moved:
                    self.logger.warning(f"Could not move file '{filepath}'. File was moved from original location to '{correct_dir}'.")
                else:
                    self.logger.warning(f"Could not move file '{filepath}'. File was NOT moved from original location.")

            self.log_info_norepeat(f"Succesfully saved original file '{filepath}' at '{correct_dir}'.")
            playsound(self.SFX_RECORD_END)
    
    def get_valid_windows(self):
        # TODO: valorant currently sets its window rect to -32000 -32000 -31000 -31000 when it is alt tabbed.
        # this breaks this code because i assume windows will be at their normal pos and width/height even if they are not focused.
        # therefore need to find better way of doing this.
        # currently, best idea i have is to implement a "priority" system for special windows
        # set discord to lowest priority and valorant to highest priority
        # then in switcher and handle_saved_file, go through special windows in order of priority
        # this would probably fix it but it is a bit of a bandaid fix
        # best possible thing would be to find a magical "fullscreen" flag that is true when a window is fullscreen, false when it isnt
        # but dont know if this exists within windows

        # a "valid window" is defined by a window that is:
        # fullscreen (does NOT have to be focused)
        # or a window that matches the "special windows" list of strings
        valid_windows_list = {}
        full_screen_rect = (0, 0, self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1))

        def win_enum_handler(hWnd, valid_windows_list):
            # below if statement does NOT mean the window is the one focused
            # this means the window has the visible bit set. 
            # this check is here to filter out weird windows that we don't care about
            if not win32gui.IsWindowVisible(hWnd): return
            
            # setup the data we care about for our window
            try:
                rect = win32gui.GetWindowRect(hWnd)
                window_name = win32gui.GetWindowText(hWnd)
                tid, pid = win32process.GetWindowThreadProcessId(hWnd) # first var is thread id, second var is process id
                proc = psutil.Process(pid)
                exe_name = Path(proc.exe()).stem + ".exe"
                class_name = win32gui.GetClassName(hWnd)
            except Exception as error:
                self.logger.warning("Could not setup data for window correctly!", exc_info=error)
                return
            obs_window_str = f"{window_name}:{class_name}:{exe_name}"
            special_scene = None
            profile = self.DEFAULT_PROFILE

            for special_window_name, scene_name in self.SPECIAL_WINDOW_SCENES.items():
                if special_window_name.lower() in obs_window_str.lower():
                    special_scene = scene_name
                    break

            for profile_name, profile_window_name_list in self.WINDOW_PROFILES.items():
                for profile_window_name in profile_window_name_list:
                    if profile_window_name.lower() in obs_window_str.lower():
                        profile = profile_name
                        break
            
            # determine if this hWnd is fullscreen
            # the reason we're subtracting the coordinate position from the resolution number 
            # is to fix an issue with multi monitor stuff
            # if that isn't present, the coordinates are 0, so no harm done
            rect_size_x = rect[2] - rect[0]
            rect_size_y = rect[3] - rect[1]
            fsr_size_x = full_screen_rect[2]
            fsr_size_y = full_screen_rect[3]
            fullscreen = False
            if rect_size_x >= fsr_size_x and rect_size_y >= fsr_size_y:
                fullscreen = True

            # check against banned strings
            window_str_safe = True
            for banned_str in self.BANNED_STRINGS:
                if banned_str.lower() in obs_window_str.lower():
                    window_str_safe = False

            focused = hWnd == win32gui.GetForegroundWindow()
            window_info_dict = {}

            if (fullscreen or special_scene) and (window_name != "") and window_str_safe:
                window_info_dict.update({
                    "hWnd": hWnd,
                    "tid": tid,
                    "pid": pid,
                    "proc": proc,
                    "exe_name": exe_name,
                    "class_name": class_name,
                    "obs_window_str": obs_window_str,
                    "focused": focused,
                    "fullscreen": fullscreen,
                    "special_scene": special_scene,
                    "profile": profile
                })
                valid_windows_list[window_name] = window_info_dict

        try:
            win32gui.EnumWindows(win_enum_handler, valid_windows_list)
        except Exception as error:
            self.logger.exception("Error while enumerating windows to use in valid windows list!")
            return False
        
        return valid_windows_list

    def switcher(self):
        interval = self.switcher_poll_interval

        while self.running:
            if not self.switcher_active:
                time.sleep(interval)
                continue

            try:
                chosen_window = self.manage_scenes()

                if chosen_window:
                    self.profile_switcher(chosen_window["obs_window_str"])
                
                self.manage_buffer_state()

                self.manage_resolution()
            except Exception as error:
                self.handle_error(error)
            
            time.sleep(interval)
            continue

    def manage_scenes(self):
        current_scene_data = self.req.get_current_program_scene()
        if current_scene_data is None: return
        current_scene = current_scene_data.scene_name

        valid_windows = self.get_valid_windows()
        # first obtain list of all focused windows
        # this SHOULD be only one window, but you never know
        focused_windows = [window for window in valid_windows.values() if window.get("focused")]
        chosen_window = None

        if not focused_windows: 
            if current_scene != self.SCENE_AFK_NAME:
                self.log_info_norepeat(f"Setting scene to {self.SCENE_AFK_NAME}.")
                self.req.set_current_program_scene(self.SCENE_AFK_NAME)
                self.afk_timer = int(time.time()) + self.buffer_timeout
            return None
        
        # separate focused windows out further into lists for special and non special focused windows
        focused_special = [window for window in focused_windows if window.get("special_scene")]
        focused_notspecial = [window for window in focused_windows if not window.get("special_scene")]
        windows_with_scene = [w for w in focused_special if w.get("special_scene")]

        game_input_settings = self.req.get_input_settings(self.GAME_CAPTURE_NAME)
        game_input_window = game_input_settings.window
        # this check fixes a potential bug if we directly alt tab from one game to another,
        # because we'd be on the same scene but the input doesn't get updated correctly
        is_gamescene_but_wrong_input_settings = ((focused_notspecial and game_input_window and current_scene == self.SCENE_GAME_NAME) and focused_notspecial[0]["obs_window_str"] != game_input_window)

        # check game capture stuff first because we prioritize games over special windows
        # we only care about the non special focused windows here
        if (current_scene != self.SCENE_GAME_NAME and focused_notspecial) or is_gamescene_but_wrong_input_settings:
            chosen_window = focused_notspecial[0]

            self.req.set_current_program_scene(self.SCENE_GAME_NAME)
            self.req.set_input_settings(
                name=self.GAME_CAPTURE_NAME, 
                settings={
                    "capture_mode": "window",
                    "window": chosen_window["obs_window_str"]
                },
                overlay=True
            )

            self.log_info_norepeat(f"Set scene to {self.SCENE_GAME_NAME}, swapped {self.SCENE_GAME_NAME} output to {chosen_window["obs_window_str"]}.")
        elif focused_special and windows_with_scene:
            if len(windows_with_scene) == 1:
                chosen_window = windows_with_scene[0]
            else:
                chosen_window = choice(windows_with_scene)
                self.logger.warning(f"Detected multiple focused special windows! Selected {chosen_window.get('obs_window_str')} to switch to at random.")
            
            target_scene = chosen_window.get("special_scene")
            
            # only switch if we're not already on the target scene
            if current_scene == target_scene: return chosen_window

            special_app_name = None
            
            for app in self.SPECIAL_WINDOW_SCENES.keys():
                if app.lower() in chosen_window.get("obs_window_str").lower():
                    special_app_name = app
                    break
            
            input_name = self.SPECIAL_WINDOWS_INPUTS.get(special_app_name)
            self.req.set_current_program_scene(target_scene)
            
            # update the input source if we have a mapping for it
            if input_name:
                self.req.set_input_settings(
                    name=input_name, 
                    settings={
                        "window": chosen_window["obs_window_str"]
                    },
                    overlay=True
                )
            self.log_info_norepeat(f"Setting scene to {target_scene}, switching {target_scene} output to {chosen_window['obs_window_str']}.")

        return chosen_window
        
    def stop_replay_buffer(self):
        interval = self.switcher_poll_interval
        time_waited = 0

        buffer_active = self.req.get_replay_buffer_status().output_active
        if buffer_active:
            self.req.stop_replay_buffer()
            while buffer_active and time_waited <= self.buffer_timeout:
                buffer_active = self.req.get_replay_buffer_status().output_active
                time.sleep(interval)
                time_waited = time_waited + interval

            return not buffer_active

    def profile_switcher(self, new_profile_name):
        profile_data = self.req.get_profile_list()
        if profile_data is None: return
        current_profile_name = profile_data.current_profile_name
        profile_list = profile_data.profiles

        if new_profile_name != current_profile_name and new_profile_name in profile_list:
            # wait for the buffer to stop before switching profiles
            stopped = self.stop_replay_buffer()
            if stopped:
                self.req.set_current_profile(new_profile_name)
                self.log_info_norepeat(f"Updated profile from {current_profile_name} to {new_profile_name}.")
            else:
                self.logger.warning(f"Could not stop buffer after waiting for {self.buffer_timeout} seconds. Profile not switched!")

    def manage_resolution(self):
        # get width and height of current display
        mon_width = self.user32.GetSystemMetrics(0)
        mon_height = self.user32.GetSystemMetrics(1)

        video_settings = self.req.get_video_settings()
        if video_settings is None: return
        obs_width = video_settings.base_width
        obs_height = video_settings.base_height

        width_same = mon_width == obs_width
        height_same = mon_height == obs_height
        if width_same and height_same: return

        stopped = self.stop_replay_buffer()
        if stopped:
            self.req.set_video_settings(
                base_width=mon_width, 
                base_height=mon_height,
                out_width = mon_width,
                out_height=mon_height,
                # leave fps alone
                numerator=video_settings.fps_numerator, denominator=video_settings.fps_denominator
                )
            self.log_info_norepeat(f"Updated OBS output from ({obs_width} x {obs_height}) to ({mon_width} x {mon_height}).")
        else:
            self.logger.warning(f"Could not stop buffer after waiting for {self.buffer_timeout} seconds. Resolution not switched!")

    def manage_buffer_state(self):
        current_scene_data = self.req.get_current_program_scene()
        if current_scene_data is None: return
        current_scene = current_scene_data.scene_name
        buffer_active = self.req.get_replay_buffer_status().output_active

        now = int(time.time())
        if current_scene == self.SCENE_AFK_NAME and now >= self.afk_timer and buffer_active:
            self.log_info_norepeat(f"Stopping replay buffer, current time '{now}' greater than afk timer '{self.afk_timer}' and replay buffer active.")
            self.req.stop_replay_buffer()
        
        elif (current_scene == self.SCENE_GAME_NAME or current_scene == self.SCENE_DISCORD_NAME) and not(buffer_active):
            self.log_info_norepeat("Starting replay buffer.")
            self.req.start_replay_buffer()

    def get_emote(self):
        previous_emote = None
        while True:
            emote = choice(self.EMOTICONS)
            if emote != previous_emote:
                yield emote
                previous_emote = emote
    
    def change_tabbed_text(self):
        interval = self.change_tabbed_text_poll_interval

        while self.running:
            try:
                current_scene_data = self.req.get_current_program_scene()
                if current_scene_data is None:
                    time.sleep(interval) 
                    continue
                current_scene = current_scene_data.scene_name

                if current_scene == self.SCENE_AFK_NAME:
                    self.req.set_input_settings("Alt Tabbed Text", {
                        "text": f"Alt Tabbed {next(self.emote_gen)}"
                    }, True)
            except Exception as error:
                self.handle_error(error)

            time.sleep(interval)
            continue

    def instant_replay(self):
        try:
            buffer_active = self.req.get_replay_buffer_status().output_active
            if buffer_active and not self.instant_replay_requested:
                self.req.save_replay_buffer()
                self.instant_replay_requested = True
            else:
                playsound(self.SFX_RECORD_ERROR)
                if not buffer_active:
                    self.logger.warning("Cannot show instant replay; replay buffer inactive!")
                elif self.instant_replay_requested:
                    self.logger.warning("Cannot show instant replay; one was already requested recently!") 
        except Exception as error:
            self.handle_error(error)

    def run(self):
        # start the text changer
        if self.change_tabbed_text_thread is None:
            self.change_tabbed_text_thread = threading.Thread(target=self.change_tabbed_text)
            self.change_tabbed_text_thread.start()
        
        # start the switcher
        if self.switcher_thread is None:
            self.switcher_thread = threading.Thread(target=self.switcher)
            self.switcher_thread.start()

        self.init_commands_observer()

        # keep the main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("Shutting down due to KeyboardInterrupt...")
            self.running = False
            self.commands_observer.stop()
            self.commands_observer.join()
            if self.change_tabbed_text_thread:
                self.change_tabbed_text_thread.join(timeout=5)
            if self.switcher_thread:
                self.switcher_thread.join(timeout=5)
            self.logger.info("Shutdown complete.")

if __name__ == "__main__":
    ms = MegaScript()
    ms.run()