import obsws_python as obs
from obsws_python import error as obserror
from random import choice
from playsound import playsound
import ctypes as ct
from pathlib import Path
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from send2trash import send2trash
from dupefilter import DuplicateFilter
from serializedreqclient import SerializedReqClient
import win32gui, win32process, time, threading, psutil, shutil, logging, re, os, json, subprocess, websocket

class MegaScript:

    ### INITIALIZATION AND HELPER FUNCTIONS
    def __init__(self):
        self.evt = None
        self.req = None
        self.req_lock = threading.RLock()

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
            "300FPS": ["valorant", "cs2", "siege", "deadlock"],
            "current": ["discord"]
        }
        self.DEFAULT_PROFILE = "60FPS"
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

        # setup tracking for profile readiness in OBS (has the profile actually changed?)
        self.profile_ready = threading.Event()
        self.profile_switch_lock = threading.Lock()
        self.profile_ready.set()
        self.profile_change_timeout = 15
        self.profile_settle_delay = 1

        # track replay buffer state through obs event callback
        self.buffer_state = "UNKNOWN"
        self.buffer_state_condition = threading.Condition()
        self.buffer_start_timeout = 10

        # prevent replay buffer requests during video reconfiguration
        self.video_reconfiguring = threading.Event()
        self.video_settle_delay = 1

        # solution for getting script running dir stolen from here:
        # https://stackoverflow.com/a/9350788
        self.script_path = Path(os.path.dirname(os.path.realpath(__file__)))

        self.modified_times = {}

        self.connected = False
        self.running = True

        # configure logger
        self.logger = logging.getLogger("obs-megascript")
        self.log_handler = logging.FileHandler(
            filename=Path.joinpath(self.script_path, "megascript.log"),
            mode="a"
        )
        self.log_formatter = logging.Formatter(
            fmt='%(asctime)s %(levelname)s %(module)s - %(funcName)s: %(message)s',
            datefmt='%Y-%m-%d %I:%M:%S %p'
        )
        self.log_handler.addFilter(DuplicateFilter())
        self.log_handler.setFormatter(self.log_formatter)
        self.logger.addHandler(self.log_handler)
        self.logger.setLevel(logging.INFO)

        # disable base obsws_python logging
        logging.getLogger("obsws_python").setLevel(logging.CRITICAL)

        self.commands_path = Path.joinpath(self.script_path, "commands.json")
        self.commands_observer = None
        self.commands_event = None

        self.instant_replay_requested = False

        self.SFX_RECORD_START = str(Path.joinpath(self.script_path, "assets", "recordingstartbeep.mp3"))
        self.SFX_RECORD_END = str(Path.joinpath(self.script_path, "assets", "recordingendbeep.mp3"))
        self.SFX_RECORD_ERROR = str(Path.joinpath(self.script_path, "assets", "error.mp3"))
        self.SFX_COMMAND_RECEIVED = str(Path.joinpath(self.script_path, "assets", "commandreceived.mp3"))

        self.logger.info("Megascript initialized! Connecting to OBS...")
        self.reset_commands_json()
        self.establish_connection()
        self.profile_switcher(self.DEFAULT_PROFILE)
        self.manage_resolution()
        self.logger.info("Connected to OBS!")

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
                                    self.logger.info(f"Toggling switcher to {self.switcher_active}.")
                                    commands_data["toggleSwitcher"] = False
                                
                                if commands_data["instantReplay"]:
                                    playsound(self.SFX_COMMAND_RECEIVED)
                                    self.logger.info("Attempting to initiate instant replay...")
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

    def establish_connection(self):
        while not self.connected:
            try:
                # this fixes a potential issue with sharing the same request client instance across threads
                # SerializedReqClient employs a lock to ensure each request doesn't interfere with one another
                # i could provide a separate instance of obs.ReqClient per thread, but that would be more annoying to implement 

                # ai written explanation for this:
                # The obsws-python request client uses a shared WebSocket connection and performs its own send/receive loop. 
                # Its implementation does not provide a lock around that complete operation. 
                # Concurrent requests can therefore consume each other's responses.
                self.req = SerializedReqClient(
                    obs.ReqClient(),
                    self.req_lock
                )
                self.evt = obs.EventClient()

                # run some test funcs to make sure we actually have a connection to obs
                self.req.get_version()
                self.req.get_current_program_scene()
                self.req.get_stats()

                # at this point if we haven't thrown an error we're probably chill
                # set up the event callbacks and set connected to true
                self.profile_ready.set()
                self.evt.callback.register(self.on_replay_buffer_saved)
                self.evt.callback.register(self.on_replay_buffer_state_changed)
                self.evt.callback.register(self.on_current_profile_changing)
                self.evt.callback.register(self.on_current_profile_changed)
                self.evt.callback.register(self.on_record_state_changed)

                # manually grab buffer state for the first time (no event to update it, we have to record it ourselves here)
                with self.buffer_state_condition:
                    status = self.req.get_replay_buffer_status()
                    if status.output_active:
                        self.buffer_state = "OBS_WEBSOCKET_OUTPUT_STARTED"
                    else:
                        self.buffer_state = "OBS_WEBSOCKET_OUTPUT_STOPPED"
                    self.buffer_state_condition.notify_all()

                self.connected = True
            except Exception:
                self.logger.exception("Could not establish connection to OBS!")
                time.sleep(self.connect_attempts_interval)

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

    def get_buffer_state(self):
        # use our condition locking mechanism whenever we access the buffer state
        with self.buffer_state_condition:
            return self.buffer_state

    # END INITIALIZATION AND HELPER FUNCTIONS



    ### ERROR HANDLING

    def handle_error(self, error):
        if isinstance(error, (
            obserror.OBSSDKTimeoutError, 
            json.JSONDecodeError, 
            ConnectionAbortedError,
            ConnectionResetError,
            ConnectionRefusedError,
            BrokenPipeError,
            websocket.WebSocketConnectionClosedException,
        )) or isinstance(error, obserror.OBSSDKRequestError) and error.code == 207:
            self.handle_connection_lost(error)
        else:
            self.logger.error(
                "OBS controller error: %s",
                error,
                exc_info=(type(error), error, error.__traceback__)
            )

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
            
            self.logger.warning("OBS connection failed, reconnecting...")

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

            self.logger.info(f"Reconnected to OBS!")


    ### END ERROR HANDLING



    ### EVENT CALLBACK DEFINITIONS

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

    def on_replay_buffer_state_changed(self, data):
        with self.buffer_state_condition:
            self.buffer_state = data.output_state
            self.buffer_state_condition.notify_all()

        self.logger.info(f"Replay buffer state changed: {data.output_state}")

    def on_current_profile_changing(self, data):
        self.profile_ready.clear()

        self.logger.info(f"OBS profile change started: {data.profile_name}")

    def on_current_profile_changed(self, data):
        self.logger.info(f"OBS profile change completed: {data.profile_name}")

        self.profile_ready.set()

    ### END EVENT CALLBACK DEFINITIONS



    ### FILE SAVING

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
                self.logger.info(f"Sent '{filepath}' to trash after user exited instant replay successfully!")
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

            self.logger.info(f"Succesfully saved original file '{filepath}' at '{correct_dir}'.")
            playsound(self.SFX_RECORD_END)

    ### END FILE SAVING



    ### SWITCHER DEFINITIONS

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
            if not self.profile_ready.is_set():
                time.sleep(interval)
                continue

            try:
                chosen_window = self.manage_scenes()
                profile_ok = True

                if chosen_window:
                    profile_ok = self.profile_switcher(chosen_window["profile"])

                # only continue if profile switching worked
                if profile_ok and self.profile_ready.is_set():
                    self.manage_resolution()

                    if self.profile_ready.is_set() and not self.video_reconfiguring.is_set():
                        self.manage_buffer_state()

            except Exception as error:
                self.handle_error(error)

            time.sleep(interval)

    ### END SWITCHER DEFINITIONS



    ### OBS STATE MANAGERS

    def manage_scenes(self):
        if not self.profile_ready.is_set(): return

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
                self.logger.info(f"Setting scene to {self.SCENE_AFK_NAME}.")
                self.req.set_current_program_scene(self.SCENE_AFK_NAME)
                self.afk_timer = int(time.time()) + self.buffer_timeout
            return None
        
        # separate focused windows out further into lists for special and non special focused windows
        focused_special = [window for window in focused_windows if window.get("special_scene")]
        focused_notspecial = [window for window in focused_windows if not window.get("special_scene")]
        windows_with_scene = [w for w in focused_special if w.get("special_scene")]

        game_capture_data = self.req.get_input_settings(self.GAME_CAPTURE_NAME)
        game_input_window = None
        if game_capture_data: 
            game_input_window = game_capture_data.input_settings["window"]
        
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

            self.logger.info(f"Set scene to {self.SCENE_GAME_NAME}, swapped {self.SCENE_GAME_NAME} output to {chosen_window["obs_window_str"]}.")
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
            self.logger.info(f"Setting scene to {target_scene}, switching {target_scene} output to {chosen_window['obs_window_str']}.")

        return chosen_window
    
    def profile_switcher(self, new_profile_name):
        # only allow one profile switch operation at a time (if multiple threads were to request a switch)
        if not self.profile_switch_lock.acquire(blocking=False):
            self.logger.info(f"Profile switch already in progress! Ignoring request for profile {new_profile_name}.")
            return False

        try:
            # check if obs is already switching profiles
            if not self.profile_ready.is_set():
                self.logger.warning(f"OBS is not ready to swap profiles! Aborting request for profile switch to {new_profile_name}.")
                return False

            # ensure we can read our profile data first
            profile_data = self.req.get_profile_list()
            if profile_data is None:
                return False

            current_profile_name = profile_data.current_profile_name
            profile_list = profile_data.profiles

            if new_profile_name == current_profile_name:
                return True

            if new_profile_name not in profile_list:
                return False

            # wait for the buffer to stop before switching
            stopped = self.stop_replay_buffer()

            if not stopped:
                self.logger.warning(f"Could not stop replay buffer! Profile change to {new_profile_name} aborted.")
                return False

            self.logger.info(f"Switching profile from {current_profile_name} to {new_profile_name}.")

            # mark OBS as unavailable BEFORE the request is sent
            self.profile_ready.clear()

            try:
                self.req.set_current_profile(new_profile_name)

            except Exception:
                self.logger.exception("Failed to request OBS profile change!")
                return False

            # wait for CurrentProfileChanged to set profile_ready again
            if not self.profile_ready.wait(timeout=self.profile_change_timeout):
                self.logger.error("Timed out waiting for OBS profile change!")
                return False

            time.sleep(self.profile_settle_delay)

            self.logger.info(f"Updated profile from {current_profile_name} to {new_profile_name}.")

            return True

        finally:
            # always release the lock regardless of any errors
            self.profile_switch_lock.release()

    def manage_resolution(self):
        if not self.profile_ready.is_set() or self.video_reconfiguring.is_set():
            return

        self.video_reconfiguring.set()

        try:
            # get width and height of current display
            mon_width = self.user32.GetSystemMetrics(0)
            mon_height = self.user32.GetSystemMetrics(1)

            video_settings = self.req.get_video_settings()
            if video_settings is None: return

            obs_base = (video_settings.base_width, video_settings.base_height)
            obs_output = (video_settings.output_width, video_settings.output_height)
            target_resolution = (mon_width, mon_height)

            if obs_base == target_resolution and obs_output == target_resolution:
                return

            stopped = self.stop_replay_buffer()
            if not stopped:
                self.logger.warning("Could not stop replay buffer. Resolution not switched!")
                return

            self.req.set_video_settings(
                base_width=mon_width,
                base_height=mon_height,
                out_width=mon_width,
                out_height=mon_height,
                # leave fps alone
                numerator=video_settings.fps_numerator,
                denominator=video_settings.fps_denominator
            )

            self.logger.info(f"Updated OBS output from ({obs_base[0]} x {obs_base[1]}) to ({mon_width} x {mon_height}).")

            # allow obs some time to finish setting up video settings before starting output
            time.sleep(self.video_settle_delay)

        finally:
            # always release the lock regardless of any errors
            self.video_reconfiguring.clear()

    ### END OBS STATE MANAGERS



    ### REPLAY BUFFER SPECIFIC MANAGERS

    def wait_for_buffer_state(self, expected_states, timeout):
        # this is the entire reason this is a Condition instead of a lock:
        # we can wait for the buffer to be in the state we want, which is only possible with a condition
        with self.buffer_state_condition:
            return self.buffer_state_condition.wait_for(lambda: self.buffer_state in expected_states, timeout=timeout)

    def mark_buffer_state_unknown(self, expected_states):
        with self.buffer_state_condition:
            if self.buffer_state not in expected_states:
                self.buffer_state = "UNKNOWN"

    def stop_replay_buffer(self):
        if not self.profile_ready.is_set():
            return False

        buffer_state = self.get_buffer_state()

        if buffer_state == "UNKNOWN":
            self.logger.warning("Replay buffer state is unknown! Buffer stop request dropped.")
            return False

        if buffer_state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            return True

        # wait to stop the buffer if it's already starting
        if buffer_state == "OBS_WEBSOCKET_OUTPUT_STARTING":
            if not self.wait_for_buffer_state({"OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_STOPPED"}, self.buffer_start_timeout):
                self.mark_buffer_state_unknown({
                    "OBS_WEBSOCKET_OUTPUT_STARTED",
                    "OBS_WEBSOCKET_OUTPUT_STOPPED"
                })
                self.logger.warning("Timed out waiting for replay buffer to finish starting!")
                return False

            buffer_state = self.get_buffer_state()

        if buffer_state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            return True

        # are we already in the process of stopping the buffer?
        if buffer_state == "OBS_WEBSOCKET_OUTPUT_STOPPING":
            stopped = self.wait_for_buffer_state(
                {"OBS_WEBSOCKET_OUTPUT_STOPPED"},
                self.stop_buffer_timeout
            )

            # did we time out and the buffer still hasn't been stopped?
            if not stopped:
                self.mark_buffer_state_unknown({"OBS_WEBSOCKET_OUTPUT_STOPPED"})
            return stopped

        if buffer_state != "OBS_WEBSOCKET_OUTPUT_STARTED":
            self.logger.warning(f"Unexpected replay buffer state: {buffer_state}")
            return False

        self.logger.info("Stopping replay buffer.")
        try:
            self.req.stop_replay_buffer()
        except Exception:
            self.mark_buffer_state_unknown({"OBS_WEBSOCKET_OUTPUT_STOPPED"})
            raise # throw this same exception to our main switcher logic for it to be dealt with there

        stopped = self.wait_for_buffer_state({"OBS_WEBSOCKET_OUTPUT_STOPPED"}, self.stop_buffer_timeout)

        if not stopped:
            self.mark_buffer_state_unknown({"OBS_WEBSOCKET_OUTPUT_STOPPED"})
            self.logger.warning("Timed out waiting for replay buffer to stop.")

        return stopped

    def start_replay_buffer(self):
        if not self.profile_ready.is_set() or self.video_reconfiguring.is_set():
            return False

        buffer_state = self.get_buffer_state()

        if buffer_state == "OBS_WEBSOCKET_OUTPUT_STARTED":
            return True

        if buffer_state != "OBS_WEBSOCKET_OUTPUT_STOPPED":
            return False

        self.logger.info("Starting replay buffer.")
        try:
            self.req.start_replay_buffer()
        except Exception:
            self.mark_buffer_state_unknown({"OBS_WEBSOCKET_OUTPUT_STARTED"})
            raise

        started = self.wait_for_buffer_state(
            {"OBS_WEBSOCKET_OUTPUT_STARTED"},
            self.buffer_start_timeout
        )

        if not started:
            self.mark_buffer_state_unknown({"OBS_WEBSOCKET_OUTPUT_STARTED"})
            self.logger.warning("Timed out waiting for replay buffer to start.")

        return started

    def manage_buffer_state(self):
        if not self.profile_ready.is_set() or self.video_reconfiguring.is_set():
            return

        current_scene_data = self.req.get_current_program_scene()
        if current_scene_data is None: return
        current_scene = current_scene_data.scene_name

        buffer_state = self.get_buffer_state()

        now = int(time.time())

        if (current_scene == self.SCENE_AFK_NAME and now >= self.afk_timer and buffer_state == "OBS_WEBSOCKET_OUTPUT_STARTED"):
            self.logger.info(f"Stopping replay buffer, current time '{now}' greater than afk timer '{self.afk_timer}'.")
            self.stop_replay_buffer()

        elif (current_scene in (self.SCENE_GAME_NAME, self.SCENE_DISCORD_NAME) and buffer_state == "OBS_WEBSOCKET_OUTPUT_STOPPED"):
            self.start_replay_buffer()

    def instant_replay(self):
        try:
            if not self.profile_ready.is_set() or self.video_reconfiguring.is_set():
                self.logger.warning("Rejecting instant replay request during OBS reconfiguration!")
                return

            buffer_active = self.get_buffer_state() == "OBS_WEBSOCKET_OUTPUT_STARTED"

            if buffer_active and not self.instant_replay_requested:
                # set this before sending the request to avoid a callback race
                self.instant_replay_requested = True
                try:
                    self.req.save_replay_buffer()
                except Exception:
                    self.instant_replay_requested = False
                    raise
            else:
                playsound(self.SFX_RECORD_ERROR)
                if not buffer_active:
                    self.logger.warning("Cannot show instant replay; replay buffer inactive!")
                elif self.instant_replay_requested:
                    self.logger.warning("Cannot show instant replay; one was already requested recently!")
        except Exception as error:
            self.handle_error(error)

    ### END REPLAY BUFFER SPECIFIC MANAGERS



    ### ALT TABBED TEXT CHANGER

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
                if not self.profile_ready.is_set() or self.video_reconfiguring.is_set():
                    time.sleep(interval)
                    continue

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

    ### END ALT TABBED TEXT CHANGER
        
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



# START SCRIPT
if __name__ == "__main__":
    ms = MegaScript()
    ms.run()