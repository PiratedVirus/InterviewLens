// voicerecorder -- records the microphone through macOS's Voice Processing
// I/O AudioUnit, which is what gives QuickTime/FaceTime-quality mic audio.
//
// Why this exists: ffmpeg's avfoundation input grabs the *raw* mic stream.
// Apple's noise suppression, echo cancellation and automatic gain control
// live in the voice-processing audio unit (VPIO), and "Voice Isolation" mic
// mode only applies to apps that route through it. Enabling it here is what
// removes the broadband background noise the raw ffmpeg capture picks up.
//
// Two things that are easy to get wrong and cost a lot of debugging:
//   1. VPIO is *duplex*. If you enable only the input bus it fails to
//      initialize with -10875 and never delivers a single buffer. The
//      output bus must be enabled too and fed silence.
//   2. AVAudioEngine's setVoiceProcessingEnabled: has the same underlying
//      requirement and silently delivered zero buffers in this CLI process,
//      which is why this uses the AudioUnit API directly.
//
// Written in Objective-C rather than Swift only because the installed
// Command Line Tools ship a Swift compiler that doesn't match their own SDK.
//
// Usage:  voicerecorder <output.wav>
// Records until SIGINT/SIGTERM, or until stdin is closed by the parent.
// Prints "READY" on stdout once capture has actually started.
// Set VR_NO_VP=1 to record raw (no voice processing) for A/B comparison.

#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <Foundation/Foundation.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>

static const double kSampleRate = 48000.0;
static const int kChannels = 1;
static const int kBitsPerSample = 16;

static AudioUnit gUnit;
static AudioBufferList *gRenderList;
static FILE *gFile;
static dispatch_queue_t gWriteQueue;
static volatile long gFramesWritten = 0;
static volatile int gDidShutdown = 0;
static volatile sig_atomic_t gStopRequested = 0;
static volatile long gCallbacks = 0;
static volatile long gRenderErrors = 0;
static volatile OSStatus gFirstRenderError = 0;

// Signal handlers must stay trivial; the main loop does the real teardown.
static void handle_stop_signal(int sig) {
    (void)sig;
    gStopRequested = 1;
}

// ---------------------------------------------------------------------------
// WAV header (written twice: a placeholder up front, then patched on exit
// once the final data size is known).
// ---------------------------------------------------------------------------
static void write_wav_header(FILE *f, uint32_t dataBytes) {
    const uint32_t byteRate = (uint32_t)(kSampleRate * kChannels * kBitsPerSample / 8);
    const uint16_t blockAlign = (uint16_t)(kChannels * kBitsPerSample / 8);
    const uint32_t chunkSize = 36 + dataBytes;
    const uint32_t subchunk1Size = 16;
    const uint16_t audioFormat = 1;  // PCM
    const uint16_t numChannels = kChannels;
    const uint32_t sampleRate = (uint32_t)kSampleRate;
    const uint16_t bits = kBitsPerSample;

    fwrite("RIFF", 1, 4, f);
    fwrite(&chunkSize, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f);
    fwrite(&subchunk1Size, 4, 1, f);
    fwrite(&audioFormat, 2, 1, f);
    fwrite(&numChannels, 2, 1, f);
    fwrite(&sampleRate, 4, 1, f);
    fwrite(&byteRate, 4, 1, f);
    fwrite(&blockAlign, 2, 1, f);
    fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f);
    fwrite(&dataBytes, 4, 1, f);
}

// The built-in mic often sits at 96kHz while the speakers run at 48kHz.
// VPIO is a single duplex unit, and that mismatch makes it deliver a
// fraction of a second of audio and then silently stall. Pin the input
// device to 48kHz so both sides agree. (This changes the device's nominal
// rate system-wide; 48kHz is the sane default for voice and can be changed
// back in Audio MIDI Setup.)
static void align_input_sample_rate(void) {
    AudioObjectPropertyAddress devAddr = {kAudioHardwarePropertyDefaultInputDevice,
                                          kAudioObjectPropertyScopeGlobal,
                                          kAudioObjectPropertyElementMain};
    AudioDeviceID dev = kAudioObjectUnknown;
    UInt32 size = sizeof(dev);
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &devAddr, 0, NULL, &size, &dev) != noErr ||
        dev == kAudioObjectUnknown) {
        return;
    }

    AudioObjectPropertyAddress rateAddr = {kAudioDevicePropertyNominalSampleRate,
                                           kAudioObjectPropertyScopeGlobal,
                                           kAudioObjectPropertyElementMain};
    Float64 current = 0;
    size = sizeof(current);
    if (AudioObjectGetPropertyData(dev, &rateAddr, 0, NULL, &size, &current) == noErr &&
        current == kSampleRate) {
        return;
    }

    Float64 target = kSampleRate;
    OSStatus st = AudioObjectSetPropertyData(dev, &rateAddr, 0, NULL, sizeof(target), &target);
    fprintf(stderr, "input device rate: %.0fHz -> %.0fHz (%s)\n", current, target,
            st == noErr ? "ok" : "failed");
    if (st == noErr) {
        // The HAL needs a moment to settle before the unit is initialized.
        usleep(300000);
    }
}

static void finish(void) {
    if (gDidShutdown) return;
    gDidShutdown = 1;

    AudioOutputUnitStop(gUnit);
    AudioUnitUninitialize(gUnit);

    // Drain anything still queued, then patch the header with real sizes.
    dispatch_sync(gWriteQueue, ^{
        if (gFile) {
            uint32_t dataBytes = (uint32_t)(gFramesWritten * kChannels * kBitsPerSample / 8);
            fflush(gFile);
            fseek(gFile, 0, SEEK_SET);
            write_wav_header(gFile, dataBytes);
            fclose(gFile);
            gFile = NULL;
        }
    });

    fprintf(stderr, "callbacks %ld | render errors %ld (first %d) | captured %ld frames (%.1fs)\n",
            gCallbacks, gRenderErrors, (int)gFirstRenderError, gFramesWritten,
            gFramesWritten / kSampleRate);
    exit(0);
}

// ---------------------------------------------------------------------------
// Audio callbacks
// ---------------------------------------------------------------------------

// VPIO's output bus has to render something; give it silence.
static OSStatus SilenceCB(void *inRefCon, AudioUnitRenderActionFlags *ioActionFlags,
                          const AudioTimeStamp *inTimeStamp, UInt32 inBusNumber,
                          UInt32 inNumberFrames, AudioBufferList *ioData) {
    (void)inRefCon;
    (void)inTimeStamp;
    (void)inBusNumber;
    (void)inNumberFrames;
    for (UInt32 i = 0; ioData && i < ioData->mNumberBuffers; i++) {
        memset(ioData->mBuffers[i].mData, 0, ioData->mBuffers[i].mDataByteSize);
    }
    if (ioActionFlags) *ioActionFlags |= kAudioUnitRenderAction_OutputIsSilence;
    return noErr;
}

static OSStatus InputCB(void *inRefCon, AudioUnitRenderActionFlags *ioActionFlags,
                        const AudioTimeStamp *inTimeStamp, UInt32 inBusNumber,
                        UInt32 inNumberFrames, AudioBufferList *ioData) {
    (void)inRefCon;
    (void)ioData;

    gCallbacks++;
    const UInt32 bytes = inNumberFrames * kChannels * kBitsPerSample / 8;
    gRenderList->mNumberBuffers = 1;
    gRenderList->mBuffers[0].mNumberChannels = kChannels;
    gRenderList->mBuffers[0].mDataByteSize = bytes;

    OSStatus st = AudioUnitRender(gUnit, ioActionFlags, inTimeStamp, inBusNumber, inNumberFrames,
                                  gRenderList);
    if (st != noErr) {
        gRenderErrors++;
        if (gFirstRenderError == 0) gFirstRenderError = st;
        return noErr;  // drop a bad buffer rather than die mid-interview
    }

    // Copy off the realtime thread before touching the filesystem.
    void *copy = malloc(bytes);
    if (!copy) return noErr;
    memcpy(copy, gRenderList->mBuffers[0].mData, bytes);

    dispatch_async(gWriteQueue, ^{
      if (gFile) {
          fwrite(copy, 1, bytes, gFile);
          gFramesWritten += inNumberFrames;
      }
      free(copy);
    });
    return noErr;
}

// ---------------------------------------------------------------------------
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "usage: voicerecorder <output.wav>\n");
            return 2;
        }
        const BOOL useVoiceProcessing = (getenv("VR_NO_VP") == NULL);

        // A bare CLI binary has no bundle, so make the TCC state explicit --
        // otherwise a permission problem looks like a code bug.
        AVAuthorizationStatus authStatus =
            [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
        if (authStatus == AVAuthorizationStatusNotDetermined) {
            dispatch_semaphore_t sem = dispatch_semaphore_create(0);
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio
                                     completionHandler:^(BOOL granted) {
                                       (void)granted;
                                       dispatch_semaphore_signal(sem);
                                     }];
            dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC));
            authStatus = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
        }
        if (authStatus != AVAuthorizationStatusAuthorized) {
            fprintf(stderr, "error: microphone access not granted (status %ld)\n", (long)authStatus);
            return 1;
        }

        gFile = fopen(argv[1], "wb");
        if (!gFile) {
            fprintf(stderr, "error: cannot open %s\n", argv[1]);
            return 1;
        }
        write_wav_header(gFile, 0);  // placeholder, patched in finish()
        gWriteQueue = dispatch_queue_create("voicerecorder.write", DISPATCH_QUEUE_SERIAL);

        align_input_sample_rate();

        AudioComponentDescription desc = {
            .componentType = kAudioUnitType_Output,
            .componentSubType = useVoiceProcessing ? kAudioUnitSubType_VoiceProcessingIO
                                                   : kAudioUnitSubType_HALOutput,
            .componentManufacturer = kAudioUnitManufacturer_Apple,
        };
        AudioComponent comp = AudioComponentFindNext(NULL, &desc);
        if (!comp || AudioComponentInstanceNew(comp, &gUnit) != noErr) {
            fprintf(stderr, "error: cannot create audio unit\n");
            return 1;
        }

        UInt32 one = 1, zero = 0;
        OSStatus st;
        st = AudioUnitSetProperty(gUnit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1,
                                  &one, sizeof(one));
        if (st != noErr) {
            fprintf(stderr, "error: enable input failed (%d)\n", (int)st);
            return 1;
        }
        // VPIO is duplex: without the output bus it fails to initialize and
        // delivers nothing. HALOutput is happy input-only.
        UInt32 enableOutput = useVoiceProcessing ? one : zero;
        st = AudioUnitSetProperty(gUnit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0,
                                  &enableOutput, sizeof(enableOutput));
        if (st != noErr) {
            fprintf(stderr, "error: set output IO failed (%d)\n", (int)st);
            return 1;
        }

        AudioStreamBasicDescription asbd = {0};
        asbd.mSampleRate = kSampleRate;
        asbd.mFormatID = kAudioFormatLinearPCM;
        asbd.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
        asbd.mChannelsPerFrame = kChannels;
        asbd.mBitsPerChannel = kBitsPerSample;
        asbd.mFramesPerPacket = 1;
        asbd.mBytesPerFrame = kChannels * kBitsPerSample / 8;
        asbd.mBytesPerPacket = asbd.mBytesPerFrame;

        st = AudioUnitSetProperty(gUnit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1,
                                  &asbd, sizeof(asbd));
        if (st != noErr) {
            fprintf(stderr, "error: set input format failed (%d)\n", (int)st);
            return 1;
        }
        if (useVoiceProcessing) {
            AudioUnitSetProperty(gUnit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0,
                                 &asbd, sizeof(asbd));
            AURenderCallbackStruct rcb = {.inputProc = SilenceCB, .inputProcRefCon = NULL};
            AudioUnitSetProperty(gUnit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0,
                                 &rcb, sizeof(rcb));
        }

        AURenderCallbackStruct cb = {.inputProc = InputCB, .inputProcRefCon = NULL};
        st = AudioUnitSetProperty(gUnit, kAudioOutputUnitProperty_SetInputCallback,
                                  kAudioUnitScope_Global, 0, &cb, sizeof(cb));
        if (st != noErr) {
            fprintf(stderr, "error: set input callback failed (%d)\n", (int)st);
            return 1;
        }

        gRenderList = malloc(sizeof(AudioBufferList) + sizeof(AudioBuffer));
        gRenderList->mNumberBuffers = 1;
        gRenderList->mBuffers[0].mNumberChannels = kChannels;
        gRenderList->mBuffers[0].mDataByteSize = 65536;
        gRenderList->mBuffers[0].mData = malloc(65536);

        st = AudioUnitInitialize(gUnit);
        if (st != noErr) {
            fprintf(stderr, "error: AudioUnitInitialize failed (%d)\n", (int)st);
            return 1;
        }
        st = AudioOutputUnitStart(gUnit);
        if (st != noErr) {
            fprintf(stderr, "error: AudioOutputUnitStart failed (%d)\n", (int)st);
            return 1;
        }

        fprintf(stderr, "voice processing: %s | %dHz mono 16-bit\n", useVoiceProcessing ? "ON" : "OFF",
                (int)kSampleRate);
        printf("READY\n");
        fflush(stdout);

        // Parent stops us by closing stdin (or sending a signal). The main
        // thread deliberately just polls a flag rather than parking in
        // dispatch_main() -- under dispatch_main() the audio unit never
        // delivered input callbacks in this process.
        signal(SIGINT, handle_stop_signal);
        signal(SIGTERM, handle_stop_signal);

        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
          char buf[256];
          while (fgets(buf, sizeof(buf), stdin) != NULL) {
              if (buf[0] == 'q') break;
          }
          gStopRequested = 1;
        });

        while (!gStopRequested) {
            usleep(100000);
        }
        finish();
    }
    return 0;
}
