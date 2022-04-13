#!/usr/bin/env python

import argparse
import time
import sys
from sys import exit
from Emulator import Emulator
from Evmapy import Evmapy
import generators as generators
from generators.yuzu.yuzuGenerator import YuzuGenerator
from generators.yuzuearlyaccess.yuzuearlyaccessGenerator import YuzuEarlyAccessGenerator
from generators.ryujinx.ryujinxGenerator import RyujinxGenerator
from generators.ryujinxvulkan.ryujinxvulkanGenerator import RyujinxVulkanGenerator

import controllersConfig as controllers
import batoceraFiles as batoceraFiles
import signal
import os
import subprocess
import utils.videoMode as videoMode
from utils.logger import get_logger

eslog = get_logger(__name__)

generators = {
    'yuzu': YuzuGenerator(),
    'ryujinx': RyujinxGenerator(),
    'ryujinx-vulkan': RyujinxVulkanGenerator(),
    'yuzu-early-access': YuzuEarlyAccessGenerator(),
}

def main(args, maxnbplayers):
    playersControllers = dict()

    controllersInput = []
    for p in range(1, maxnbplayers+1):
        ci = {}
        ci["index"]      = getattr(args, "p{}index"     .format(p))
        ci["guid"]       = getattr(args, "p{}guid"      .format(p))
        ci["name"]       = getattr(args, "p{}name"      .format(p))
        ci["devicepath"] = getattr(args, "p{}devicepath".format(p))
        ci["nbbuttons"]  = getattr(args, "p{}nbbuttons" .format(p))
        ci["nbhats"]     = getattr(args, "p{}nbhats"    .format(p))
        ci["nbaxes"]     = getattr(args, "p{}nbaxes"    .format(p))
        controllersInput.append(ci)

    # Read the controller configuration
    playersControllers = controllers.loadControllerConfig(controllersInput)
    # find the system to run
    systemName = args.system
    eslog.debug("Running system: {}".format(systemName))
    system = Emulator(systemName, args.rom)
    eslog.debug("Emulator system: {}".format(system))

    if args.emulator is not None:
        system.config["emulator"] = args.emulator
        system.config["emulator-forced"] = True
    if args.core is not None:
        system.config["core"] = args.core
        system.config["core-forced"] = True
    debugDisplay = system.config.copy()
    if "retroachievements.password" in debugDisplay:
        debugDisplay["retroachievements.password"] = "***"
    eslog.debug("Settings: {}".format(debugDisplay))
    if "emulator" in system.config and "core" in system.config:
        eslog.debug("emulator: {}, core: {}".format(system.config["emulator"], system.config["core"]))
    else:
        if "emulator" in system.config:
            eslog.debug("emulator: {}".format(system.config["emulator"]))

    # enable mouse
    subprocess.run(["unclutter-remote", "-s"])
    # chmod +x F1 applications
    subprocess.call(['chmod', '-R', '+x', '/userdata/system/switch/extra'])

    # the resolution must be changed before configuration while the configuration may depend on it (ie bezels)
    wantedGameMode = generators[system.config['emulator']].getResolutionMode(system.config)
    systemMode = videoMode.getCurrentMode()

    resolutionChanged = False
    exitCode = -1
    try:
        # lower the resolution if mode is auto
        newsystemMode = systemMode # newsystemmode is the mode after minmax (ie in 1K if tv was in 4K), systemmode is the mode before (ie in es)
        if system.config["videomode"] == "" or system.config["videomode"] == "default":
            eslog.debug("minTomaxResolution")
            eslog.debug("video mode before minmax: {}".format(systemMode))
            videoMode.minTomaxResolution()
            newsystemMode = videoMode.getCurrentMode()
            if newsystemMode != systemMode:
                resolutionChanged = True

        eslog.debug("current video mode: {}".format(newsystemMode))
        eslog.debug("wanted video mode: {}".format(wantedGameMode))

        if wantedGameMode != 'default' and wantedGameMode != newsystemMode:
            videoMode.changeMode(wantedGameMode)
            resolutionChanged = True
        gameResolution = videoMode.getCurrentResolution()

        # if resolution is reversed (ie ogoa boards), reverse it in the gameResolution to have it correct
        if system.isOptSet('resolutionIsReversed') and system.getOptBoolean('resolutionIsReversed') == True:
            x = gameResolution["width"]
            gameResolution["width"]  = gameResolution["height"]
            gameResolution["height"] = x
        eslog.debug("resolution: {}x{}".format(str(gameResolution["width"]), str(gameResolution["height"])))

        # savedir: create the save directory if not already done
        dirname = os.path.join(batoceraFiles.savesDir, system.name)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        # core
        effectiveCore = ""
        if "core" in system.config and system.config["core"] is not None:
            effectiveCore = system.config["core"]
        effectiveRom = ""
        if args.rom is not None:
            effectiveRom = args.rom

        # run a script before emulator starts
        callExternalScripts("/usr/share/batocera/configgen/scripts", "gameStart", [systemName, system.config['emulator'], effectiveCore, effectiveRom])
        callExternalScripts("/userdata/system/scripts", "gameStart", [systemName, system.config['emulator'], effectiveCore, effectiveRom])

        # run the emulator
        try:
            Evmapy.start(systemName, system.config['emulator'], effectiveCore, effectiveRom, playersControllers)
            # change directory if wanted
            executionDirectory = generators[system.config['emulator']].executionDirectory(system.config, effectiveRom)
            if executionDirectory is not None:
                os.chdir(executionDirectory)

            exitCode = runCommand(generators[system.config['emulator']].generate(system, args.rom, playersControllers, gameResolution))
        finally:
            Evmapy.stop()

        # run a script after emulator shuts down
        callExternalScripts("/userdata/system/scripts", "gameStop", [systemName, system.config['emulator'], effectiveCore, effectiveRom])
        callExternalScripts("/usr/share/batocera/configgen/scripts", "gameStop", [systemName, system.config['emulator'], effectiveCore, effectiveRom])
   
    finally:
        # always restore the resolution
        if resolutionChanged:
            try:
                videoMode.changeMode(systemMode)
            except Exception:
                pass # don't fail
    # exit
    return exitCode

def callExternalScripts(folder, event, args):
    if not os.path.isdir(folder):
        return

    for file in os.listdir(folder):
        if os.path.isdir(os.path.join(folder, file)):
            callExternalScripts(os.path.join(folder, file), event, args)
        else:
            if os.access(os.path.join(folder, file), os.X_OK):
                eslog.debug("calling external script: " + str([os.path.join(folder, file), event] + args))
                subprocess.call([os.path.join(folder, file), event] + args)

def runCommand(command):
    global proc

    command.env.update(os.environ)
    eslog.debug("command: {}".format(str(command)))
    eslog.debug("command: {}".format(str(command.array)))
    eslog.debug("env: {}".format(str(command.env)))
    proc = subprocess.Popen(command.array, env=command.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    exitcode = -1
    try:
        out, err = proc.communicate()
        exitcode = proc.returncode
        sys.stdout.write(out)
        sys.stderr.write(err)
    except:
        eslog.debug("emulator exited")

    return exitcode

def signal_handler(signal, frame):
    global proc
    print('Exiting')
    if proc:
        print('killing proc')
        proc.kill()

if __name__ == '__main__':
    proc = None
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description='emulator-launcher script')

    maxnbplayers = 8
    for p in range(1, maxnbplayers+1):
        parser.add_argument("-p{}index"     .format(p), help="player{} controller index"            .format(p), type=int, required=False)
        parser.add_argument("-p{}guid"      .format(p), help="player{} controller SDL2 guid"        .format(p), type=str, required=False)
        parser.add_argument("-p{}name"      .format(p), help="player{} controller name"             .format(p), type=str, required=False)
        parser.add_argument("-p{}devicepath".format(p), help="player{} controller device"           .format(p), type=str, required=False)
        parser.add_argument("-p{}nbbuttons" .format(p), help="player{} controller number of buttons".format(p), type=str, required=False)
        parser.add_argument("-p{}nbhats"    .format(p), help="player{} controller number of hats"   .format(p), type=str, required=False)
        parser.add_argument("-p{}nbaxes"    .format(p), help="player{} controller number of axes"   .format(p), type=str, required=False)
    
    parser.add_argument("-system", help="select the system to launch", type=str, required=True)
    parser.add_argument("-rom", help="rom absolute path", type=str, required=True)
    parser.add_argument("-emulator", help="force emulator", type=str, required=False)
    parser.add_argument("-core", help="force emulator core", type=str, required=False)

    args = parser.parse_args()
    try:
        exitcode = -1
        exitcode = main(args, maxnbplayers)
    except Exception as e:
        eslog.error("configgen exception: ", exc_info=True)
    time.sleep(1) # this seems to be required so that the gpu memory is restituated and available for es
    eslog.debug("Exiting configgen with status {}".format(str(exitcode)))
    exit(exitcode)

# Local Variables:
# tab-width:4
# indent-tabs-mode:nil
# End:
# vim: set expandtab tabstop=4 shiftwidth=4:
