"""
文件说明：
这个文件实现了一个语音识别客户端。它通过连接到指定的语音识别服务器，从麦克风录制音频，然后把音频转换为文字。程序支持实时显示识别结果，并自动判断何时应结束录音。通过命令行参数可以设置很多参数，比如选择音频输入设备、指定语言、调整暂停时间等。

背后知识：
1. argparse 模块：帮助我们从命令行接收参数，让用户可以在启动程序时设置各种选项。
2. WebSocket：用来和远程服务器通信，以传递音频和接收文字。
3. 队列（deque）：用于存储最近一段时间的识别文字，结合 SequenceMatcher 来判断文字的相似度。
4. 字符串操作和时间处理：调整文字格式和判断时间间隔。

举例说明：
假设你说“你好，世界！”，程序会先收集这些声音信号，通过网络传输给服务器识别成文字，再实时显示在屏幕上。如果声音出现断续，程序还能自动判断一句话是否结束，从而决定是否结束录音。

注释风格：
以下代码中加入了很多详细的中文注释，解释每一步的作用，适合年龄较小的同学理解代码基本原理。

作者：RealtimeSTT 团队
日期：2023年10月
"""

from difflib import SequenceMatcher
from collections import deque
import argparse
import string
import shutil
import time
import sys
import os

from RealtimeSTT import AudioToTextRecorderClient
from RealtimeSTT import AudioInput

from colorama import init, Fore, Style
init()

# 定义默认的WebSocket地址，控制和数据分别使用不同端口
DEFAULT_CONTROL_URL = "ws://127.0.0.1:8011"
DEFAULT_DATA_URL = "ws://127.0.0.1:8012"

# 用来在屏幕上显示录音状态的标记，这里用红色圆点表示正在录音
recording_indicator = "🔴"

# 获取控制台的宽度，便于文本输出时做格式处理
console_width = shutil.get_terminal_size().columns

# 默认的暂停时间会在命令行中被覆盖，这里只是初始赋值
post_speech_silence_duration = 1.0  # 发言结束后等待的时间（秒）
unknown_sentence_detection_pause = 1.3  # 无法判断句子是否结束时的等待时间
mid_sentence_detection_pause = 3.0      # 当检测到中间停顿时的等待时间
end_of_sentence_detection_pause = 0.7   # 当检测到句子结束标志（如句号）时的等待时间
hard_break_even_on_background_noise = 3.0     # 在背景噪声中硬性终止的时间阈值
hard_break_even_on_background_noise_min_texts = 3         # 判定硬中断所需的最小文本数量
hard_break_even_on_background_noise_min_similarity = 0.99   # 判定文本相似度的最低比率
hard_break_even_on_background_noise_min_chars = 15        # 判定硬中断所需的最小字符数量
prev_text = ""
text_time_deque = deque()

def main():
    """
    主函数说明：
    1. 解析命令行参数，让用户可以指定设备、语言、调试模式等。
    2. 根据参数更新全局变量，设置不同的等待时间，调整录音策略。
    3. 初始化 AudioToTextRecorderClient 客户端，并设置回调函数 on_realtime_transcription_update 用于处理实时文字更新。
    4. 根据传入参数调用设置、获取或执行客户端方法，最后启动录音和识别过程。
    """
    global prev_text, post_speech_silence_duration, unknown_sentence_detection_pause
    global mid_sentence_detection_pause, end_of_sentence_detection_pause
    global hard_break_even_on_background_noise, hard_break_even_on_background_noise_min_texts
    global hard_break_even_on_background_noise_min_similarity, hard_break_even_on_background_noise_min_chars

    parser = argparse.ArgumentParser(description="STT Client")

    # 处理命令行参数：这些选项允许用户在运行程序时手动配置录音、识别和输出等参数
    parser.add_argument("-i", "--input-device", type=int, metavar="INDEX",
                        help="音频输入设备序号（用 -l 时显示设备列表）")
    parser.add_argument("-l", "--language", default="en", metavar="LANG",
                        help="使用的语言，默认为英文 (en)")
    parser.add_argument("-sed", "--speech-end-detection", action="store_true",
                        help="使用智能语音结束检测")
    parser.add_argument("-D", "--debug", action="store_true",
                        help="启用调试模式，打印调试信息")
    parser.add_argument("-n", "--norealtime", action="store_true",
                        help="禁用实时输出")
    parser.add_argument("-W", "--write", metavar="FILE",
                        help="将录制的音频保存成 WAV 文件")
    parser.add_argument("-s", "--set", nargs=2, metavar=('PARAM', 'VALUE'), action='append',
                        help="设置录音器参数（可以多次使用）")
    parser.add_argument("-m", "--method", nargs='+', metavar='METHOD', action='append',
                        help="调用录音器方法，可附加参数")
    parser.add_argument("-g", "--get", nargs=1, metavar='PARAM', action='append',
                        help="获取录音器参数的值（可以多次使用）")
    parser.add_argument("-c", "--continous", action="store_true",
                        help="不断地转录语音，而不是一次后退出")
    parser.add_argument("-L", "--list", action="store_true",
                        help="列出所有可用的音频输入设备，然后退出")
    parser.add_argument("--control", "--control_url", default=DEFAULT_CONTROL_URL,
                        help="STT 控制 WebSocket 地址")
    parser.add_argument("--data", "--data_url", default=DEFAULT_DATA_URL,
                        help="STT 数据 WebSocket 地址")
    parser.add_argument("--post-silence", type=float, default=1.0,
                      help="发音结束后静默的时长（秒），默认1.0秒")
    parser.add_argument("--unknown-pause", type=float, default=1.3,
                      help="未知句子结束检测的暂停时间（秒），默认1.3秒")
    parser.add_argument("--mid-pause", type=float, default=3.0,
                      help="句中停顿检测的暂停时间（秒），默认3.0秒")
    parser.add_argument("--end-pause", type=float, default=0.7,
                      help="句末结束检测的暂停时间（秒），默认0.7秒")
    parser.add_argument("--hard-break", type=float, default=3.0,
                      help="背景噪声中硬性中断的时间阈值（秒），默认3.0秒")
    parser.add_argument("--min-texts", type=int, default=3,
                      help="判断硬中断所需的最小文本数，默认3")
    parser.add_argument("--min-similarity", type=float, default=0.99,
                      help="判断文本相似度的最低比例，默认0.99")
    parser.add_argument("--min-chars", type=int, default=15,
                      help="判断硬中断所需的最小字符数，默认15")

    args = parser.parse_args()

    # 如果参数中要求列出设备，则调用 AudioInput 的 list_devices 方法，并结束程序
    if args.list:
        audio_input = AudioInput()
        audio_input.list_devices()
        return

    # 将命令行参数中的暂停时间和其他参数赋值给全局变量
    post_speech_silence_duration = args.post_silence
    unknown_sentence_detection_pause = args.unknown_pause
    mid_sentence_detection_pause = args.mid_pause
    end_of_sentence_detection_pause = args.end_pause
    hard_break_even_on_background_noise = args.hard_break
    hard_break_even_on_background_noise_min_texts = args.min_texts
    hard_break_even_on_background_noise_min_similarity = args.min_similarity
    hard_break_even_on_background_noise_min_chars = args.min_chars

    # 判断输出目标：如果标准输出不是终端，则认为输出重定向到文件
    if not os.isatty(sys.stdout.fileno()):
        file_output = sys.stdout
    else:
        file_output = None

    def clear_line():
        """
        clear_line 函数说明：
        这个函数用于清除终端上一行的内容。它用特殊字符 \r\033[K 实现回车并擦除当前行，确保屏幕显示更新。
        """
        if file_output:
            sys.stderr.write('\r\033[K')
        else:
            print('\r\033[K', end="", flush=True)

    def write(text):
        """
        write 函数说明：
        根据输出目标，将 text 写入标准输出或错误输出，并实时刷新屏幕。
        """
        if file_output:
            sys.stderr.write(text)
            sys.stderr.flush()
        else:
            print(text, end="", flush=True)

    def on_realtime_transcription_update(text):
        """
        on_realtime_transcription_update 函数说明：
        这是一个回调函数，用于实时处理从音频转换来的文字。主要功能：
        1. 对识别的文字进行预处理（例如删除多余空格、调整首字母大写）。
        2. 根据文字的结尾判断是否为句子结束、未结束或处于继续状态，从而调整录音停止前的等待时间。
        3. 将最近的文字及其时间存入队列中，监测是否需要硬性中断录音（例如长时间背景噪声引起的误识别）。
        4. 调用客户端的方法实现停止录音及其他控制操作。
        """
        global post_speech_silence_duration, prev_text, text_time_deque
    
        def set_post_speech_silence_duration(duration: float):
            """
            set_post_speech_silence_duration 函数说明：
            设置全局变量 post_speech_silence_duration，并调用客户端设置参数，从而改变语音识别的静默时长。
            """
            global post_speech_silence_duration
            post_speech_silence_duration = duration
            client.set_parameter("post_speech_silence_duration", duration)

        def preprocess_text(text):
            """
            preprocess_text 函数说明：
            预处理文本，把文本开头多余空格去掉，并将首个字母大写，方便阅读。
            """
            text = text.lstrip()
            if text.startswith("..."):
                text = text[3:]
            text = text.lstrip()
            if text:
                text = text[0].upper() + text[1:]
            return text

        def ends_with_ellipsis(text: str):
            """
            ends_with_ellipsis 函数说明：
            检查文本是否以省略号'...'结尾，表示说话还未完。
            """
            if text.endswith("..."):
                return True
            if len(text) > 1 and text[:-1].endswith("..."):
                return True
            return False

        def sentence_end(text: str):
            """
            sentence_end 函数说明：
            检查文本最后一个字符是否为句号、感叹号或问号，表示一句话的结束。
            """
            sentence_end_marks = ['.', '!', '?', '。']
            if text and text[-1] in sentence_end_marks:
                return True
            return False

        # 如果不是禁用实时输出，则对识别结果进行处理
        if not args.norealtime:
            # 调用预处理函数，让文字变得更整洁好看
            text = preprocess_text(text)

            if args.speech_end_detection:
                """
                使用智能语音结束检测：
                根据文本是否以省略号或句号结尾，来判断说话是否停顿或结束，进而动态调整录音前的等待时长。
                例如：如果文本以"..."结尾，说明说话还在继续，则设置较长的等待时间；
                如果当前文本和前一个文本都以句号结尾，则认为一句话结束，设置较短的等待时间；
                否则，采用默认的未知状态等待时间。
                """
                if ends_with_ellipsis(text):
                    if not post_speech_silence_duration == mid_sentence_detection_pause:
                        set_post_speech_silence_duration(mid_sentence_detection_pause)
                        if args.debug: print(f"RT: post_speech_silence_duration for {text} (...): {post_speech_silence_duration}")
                elif sentence_end(text) and sentence_end(prev_text) and not ends_with_ellipsis(prev_text):
                    if not post_speech_silence_duration == end_of_sentence_detection_pause:
                        set_post_speech_silence_duration(end_of_sentence_detection_pause)
                        if args.debug: print(f"RT: post_speech_silence_duration for {text} (.!?): {post_speech_silence_duration}")
                else:
                    if not post_speech_silence_duration == unknown_sentence_detection_pause:
                        set_post_speech_silence_duration(unknown_sentence_detection_pause)
                        if args.debug: print(f"RT: post_speech_silence_duration for {text} (???): {post_speech_silence_duration}")
                
                # 将当前处理后的文字赋值给 prev_text，用于与下次识别的文字比较
                prev_text = text

                # 记录当前文字和时间，方便后续判断相似度和是否需要停止录音
                current_time = time.time()
                text_time_deque.append((current_time, text))

                # 移除队列中超过 hard_break_even_on_background_noise 时间的数据
                while text_time_deque and text_time_deque[0][0] < current_time - hard_break_even_on_background_noise:
                    text_time_deque.popleft()

                # 如果在设定时间内的文字数达到要求，检查第一条和最后一条的相似度
                if len(text_time_deque) >= hard_break_even_on_background_noise_min_texts:
                    """
                    当环境中有持续的背景噪声（如空调声、电视声等），语音识别系统可能会不断把这些噪声错误识别为相似的文本，导致录音不会自动停止，系统一直处于等待状态。
                    这段代码通过检测一段时间内识别文本的相似度来判断是否是这种情况。如果在一段时间内（默认3秒）系统持续识别出非常相似的文本，且这些文本长度足够长，则认为是背景噪声在干扰，此时会强制停止录音，避免系统无限期等待。
                    这是一种智能防卡住机制，提高了系统的鲁棒性和用户体验。
                    """
                    texts = [t[1] for t in text_time_deque]
                    first_text = texts[0]
                    last_text = texts[-1]

                    # 使用 SequenceMatcher 计算文字相似度
                    similarity = SequenceMatcher(None, first_text, last_text).ratio()

                    # 如果相似度高，而且文字长度超过设定值，则调用客户端停止方法
                    if similarity > hard_break_even_on_background_noise_min_similarity and len(first_text) > hard_break_even_on_background_noise_min_chars:
                        client.call_method("stop")

            # 清除上一行显示的文字，为接下来的输出腾出空间
            clear_line()

            # 将文本分割为单词，选取末尾适合显示的部分，以适应控制台宽度
            words = text.split()
            last_chars = ""
            available_width = console_width - 5
            for word in reversed(words):
                if len(last_chars) + len(word) + 1 > available_width:
                    break
                last_chars = word + " " + last_chars
            last_chars = last_chars.strip()

            # 设置黄色文字显示，附加录音标记
            colored_text = f"{Fore.YELLOW}{last_chars}{Style.RESET_ALL}{recording_indicator}\b\b"
            write(colored_text)

    # 初始化 AudioToTextRecorderClient 客户端，开始语音录制和识别
    client = AudioToTextRecorderClient(
        language=args.language,
        control_url=args.control,
        data_url=args.data,
        debug_mode=args.debug,
        on_realtime_transcription_update=on_realtime_transcription_update,
        use_microphone=True,
        input_device_index=args.input_device,  # 传入用户指定的音频输入设备序号
        output_wav_file = args.write or None,
    )

    # 根据命令行参数设置相关录音器参数
    if args.set:
        for param, value in args.set:
            try:
                if '.' in value:
                    value = float(value)
                else:
                    value = int(value)
            except ValueError:
                pass  # 如果转换失败，则保持字符串形式
            client.set_parameter(param, value)

    if args.get:
        for param_list in args.get:
            param = param_list[0]
            value = client.get_parameter(param)
            if value is not None:
                print(f"Parameter {param} = {value}")

    if args.method:
        for method_call in args.method:
            method = method_call[0]
            args_list = method_call[1:] if len(method_call) > 1 else []
            client.call_method(method, args=args_list)

    # 开始语音转文字的录制和实时显示
    try:
        while True:
            if not client._recording:
                print("Recording stopped due to an error.", file=sys.stderr)
                break
            
            if not file_output:
                print(recording_indicator, end="", flush=True)
            else:
                sys.stderr.write(recording_indicator)
                sys.stderr.flush()
                
            text = client.text()

            if text and client._recording and client.is_running:
                if file_output:
                    print(text, file=file_output)
                    sys.stderr.write('\r\033[K')
                    sys.stderr.write(f'{text}')
                else:
                    print('\r\033[K', end="", flush=True)
                    print(f'{text}', end="", flush=True)
                if not args.continous:
                    break
            else:
                time.sleep(0.1)
            
            if args.continous:
                print()
            prev_text = ""
    except KeyboardInterrupt:
        print('\r\033[K', end="", flush=True)
    finally:
        client.shutdown()

if __name__ == "__main__":
    main()
