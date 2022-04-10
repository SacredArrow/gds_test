import asyncio
import json
import os
import threading

import requests
import queue
import websockets
from multiprocessing.dummy import Pool as ThreadPool

import settings


def check_ranges_status_code(url):  # Проверяем, поддерживаются ли ranges
    headers = {'Range': 'bytes=%d-%d' % (0, 1)}
    r = requests.head(url, headers=headers, stream=True)
    return r.status_code


def download_in_thread(input_queue, output_queue, i):
    while not input_queue.empty():
        start, end, url, filename, ranges_supported = input_queue.get()
        if ranges_supported:
            headers = {'Range': 'bytes=%d-%d' % (start, end)}
        else:
            headers = {}
        print(f"Downloading bytes from {start} to {end} in thread {i}")

        r = requests.get(url, headers=headers, stream=True)


        with open(filename, "r+b") as fp:
            fp.seek(start)
            fp.write(r.content)
        output_queue.put((filename, start, end))  # Доп. информацию кладем чтобы в дальнейшем иметь возможность распознавать, какие части уже завершились

def add_to_queue(url, name, queue, max_bytes):
    r = requests.head(url)
    if name:
        file_name = name
    else:
        file_name = url.split('/')[-1]
    try:
        file_size = int(r.headers['content-length'])
    except:
        print("Invalid URL")
        return

    try:
        os.makedirs(settings.OUTPUT_DIR)
    except FileExistsError:
        pass

    ranges_code = check_ranges_status_code(url)
    if ranges_code == 206:
        ranges_supported = True
    elif ranges_code == 200:
        ranges_supported = False
    else:
        print(f"Url {url} is invalid!")
        return None

    file_name = os.path.abspath(settings.OUTPUT_DIR + file_name)
    with open(file_name, "wb") as fp:  # Создаем файл и заполняем нулями
        fp.write(b'\x00' * file_size)

    if ranges_supported:
        i = 0
        cnt = 0
        while i + max_bytes < file_size:
            queue.put((i, i + max_bytes, url, file_name, ranges_supported))
            i += max_bytes
            cnt += 1
        if i != file_size:  # Последний кусок обрезаем по размеру файла
            queue.put((i, file_size, url, file_name, ranges_supported))
            cnt += 1
        return cnt, file_name, file_size
    else:  # Если ranges не поддерживаются, то скачаем в один поток
        queue.put((0, file_size, url, file_name, ranges_supported))
        return 1, file_name, file_size



if __name__ == '__main__':
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    file = open("to_download.txt", "r")
    to_download = file.read().splitlines()
    parts = {}  # На сколько частей мы разделили файл
    sizes = {}  # Размеры файлов
    total_parts = 0  # Чтобы отслеживать, когда нужно переставать смотреть в очередь
    for file in to_download:
        splitted = file.split()
        name = None
        if len(splitted) > 1: # Если в файле указано еще и имя через пробел
            file, name = splitted
        file_info = add_to_queue(file, name, input_queue, settings.MAX_BYTES)
        if not file_info:
            continue
        parts_for_file, path, size = file_info
        parts[path] = parts_for_file
        sizes[path] = size
        total_parts += parts[path]

    threads = [threading.Thread(target=download_in_thread, args=(input_queue, output_queue, i)) for i in range(settings.NUMBER_OF_THREADS)]
    for th in threads:
        th.daemon = True
        th.start()

    ready_parts = 0
    ready_parts_by_file = {part : 0 for part in parts}


    def dump_status():
        res = []
        for filepath in ready_parts_by_file:
            filename = filepath.split('/')[-1]
            res.append({'name': filename, 'finished' : ready_parts_by_file[filepath], 'total' : parts[filepath], 'path' : filepath, 'size' : sizes[filepath]})
        return json.dumps({'n_threads' : settings.NUMBER_OF_THREADS, 'max_bytes' : settings.MAX_BYTES,'files' : res})


    async def refresh_state(websocket, path):
        global ready_parts
        await websocket.send(dump_status())
        while ready_parts < total_parts:
            filename, _, _ = output_queue.get()
            ready_parts += 1
            ready_parts_by_file[filename] += 1
            await websocket.send(dump_status())


    start_server = websockets.serve(refresh_state, '127.0.0.1', 5678)

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()
