# Copyright 2020 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import os
import queue
import re
import threading

import aiohttp

_termination_obj = object()


class FlowException(Exception):
    pass


class Flow:
    def __init__(self, termination_result_fn=lambda x, y: None):
        self._outlets = []
        self._termination_result_fn = termination_result_fn

    def to(self, outlet):
        self._outlets.append(outlet)
        return outlet

    def run(self):
        for outlet in self._outlets:
            outlet.run()

    async def do(self, element):
        raise NotImplementedError

    async def _do_downstream(self, element):
        if element is _termination_obj:
            termination_result = await self._outlets[0].do(_termination_obj)
            for i in range(1, len(self._outlets)):
                termination_result = self._termination_result_fn(
                    termination_result,
                    await self._outlets[i].do(_termination_obj))
            return termination_result
        tasks = []
        for i in range(len(self._outlets)):
            tasks.append(
                asyncio.get_running_loop().create_task(
                    self._outlets[i].do(element)))
        for task in tasks:
            await task


class FlowController:
    def __init__(self, emit_fn, await_termination_fn):
        self._emit_fn = emit_fn
        self._await_termination_fn = await_termination_fn

    def emit(self, element):
        self._emit_fn(element)

    def terminate(self):
        self.emit(_termination_obj)

    def await_termination(self):
        return self._await_termination_fn()


class Source(Flow):
    def __init__(self, buffer_size=1, **kwargs):
        super().__init__(**kwargs)
        assert buffer_size > 0, 'Buffer size must be positive'
        self._q = queue.Queue(buffer_size)
        self._termination_q = queue.Queue(1)
        self._ex = None

    async def _run_loop(self):
        loop = asyncio.get_running_loop()
        self._termination_future = asyncio.futures.Future()

        while True:
            element = await loop.run_in_executor(None, self._q.get)
            try:
                termination_result = await self._do_downstream(element)
                if element is _termination_obj:
                    self._termination_future.set_result(termination_result)
            except BaseException as ex:
                self._ex = ex
                if not self._q.empty():
                    self._q.get()
                self._termination_future.set_result(None)
                break
            if element is _termination_obj:
                break

    def _loop_thread_main(self):
        asyncio.run(self._run_loop())
        self._termination_q.put(self._ex)

    def _raise_on_error(self, ex):
        if ex:
            raise FlowException('execution error') from self._ex

    def _emit(self, element):
        self._raise_on_error(self._ex)
        self._q.put(element)
        self._raise_on_error(self._ex)

    def run(self):
        super().run()

        thread = threading.Thread(target=self._loop_thread_main)
        thread.start()

        def raise_error_or_return_termination_result():
            self._raise_on_error(self._termination_q.get())
            return self._termination_future.result()

        return FlowController(
            self._emit, raise_error_or_return_termination_result)


class UnaryFunctionFlow(Flow):
    def __init__(self, fn, **kwargs):
        super().__init__(**kwargs)
        assert callable(fn), f'Expected a callable, got {type(fn)}'
        self._is_async = asyncio.iscoroutinefunction(fn)
        self._fn = fn

    async def _call(self, element):
        res = self._fn(element)
        if self._is_async:
            res = await res
        return res

    async def _do_internal(self, element, fn_result):
        raise NotImplementedError()

    async def do(self, element):
        if element is _termination_obj:
            return await self._do_downstream(element)
        else:
            fn_result = await self._call(element)
            await self._do_internal(element, fn_result)


class Map(UnaryFunctionFlow):
    async def _do_internal(self, element, mapped_elem):
        await self._do_downstream(mapped_elem)


class Filter(UnaryFunctionFlow):
    async def _do_internal(self, element, keep):
        if keep:
            await self._do_downstream(element)


class FlatMap(UnaryFunctionFlow):
    async def _do_internal(self, element, result_elements):
        for result_element in result_elements:
            await self._do_downstream(result_element)


class Reduce(Flow):
    def __init__(self, inital_value, fn):
        super().__init__()
        assert callable(fn), f'Expected a callable, got {type(fn)}'
        self._is_async = asyncio.iscoroutinefunction(fn)
        self._fn = fn
        self._result = inital_value

    def to(self, outlet):
        raise Exception("Non-terminal Reduce")

    async def do(self, element):
        if element is _termination_obj:
            return self._result
        else:
            res = self._fn(self._result, element)
            if self._is_async:
                res = await res
            self._result = res


class NeedsV3ioAccess:
    def __init__(self, webapi=None, access_key=None):
        if not webapi:
            webapi = os.getenv('V3IO_API')
            if webapi is None:
                raise ValueError('webapi or V3IO_API must be set')

        if not re.match(r'http(s)?://', webapi):
            webapi = f'http://{webapi}'

        self._webapi_url = webapi

        if not access_key:
            access_key = os.getenv('V3IO_ACCESS_KEY')
            if access_key is None:
                raise ValueError('access_key or V3IO_ACCESS_KEY must be set')

        self._get_item_headers = {
            'X-v3io-function': 'GetItem',
            'X-v3io-session-key': access_key
        }


class JoinWithTable(Flow, NeedsV3ioAccess):
    _non_int_char_pattern = re.compile(r"[^-0-9]")

    def __init__(
        self, key_extractor, join_function, table_path, attributes='*',
            webapi=None, access_key=None, **kwargs):
        Flow.__init__(self, **kwargs)
        NeedsV3ioAccess.__init__(self, webapi, access_key)
        self._key_extractor = key_extractor
        self._join_function = join_function
        self._table_path = table_path
        self._body = json.dumps({'AttributesToGet': attributes})

        self._client_session = None

    def _parse_response(self, response_body):
        response_object = json.loads(response_body)["Item"]
        for name, type_to_value in response_object.items():
            val = None
            for typ, value in type_to_value.items():
                if typ == 'S' or typ == 'BOOL':
                    val = value
                elif typ == 'N':
                    if self._non_int_char_pattern.search(value):
                        val = float(value)
                    else:
                        val = int(value)
                else:
                    raise Exception(f'Unsupported type: {typ!r}')
            response_object[name] = val
        return response_object

    async def _worker(self):
        try:
            while True:
                response_object = None
                job = await self._q.get()
                if job is _termination_obj:
                    break
                element = job[0]
                request = job[1]
                response = await request
                response_body = await response.text()
                if response.status == 200:
                    response_object = self._parse_response(response_body)
                elif response.status == 404:
                    pass
                else:
                    raise Exception(
                        'get item. status code - '
                        f'{response.status}: {response_body}')
                if response_object:
                    joined_element = self._join_function(
                            element, response_object)
                    await self._do_downstream(joined_element)
        except BaseException as ex:
            if not self._q.empty():
                await self._q.get()
            raise ex
        finally:
            await self._client_session.close()

    def _lazy_init(self):
        connector = aiohttp.TCPConnector()
        self._client_session = aiohttp.ClientSession(connector=connector)
        self._q = asyncio.queues.Queue(8)
        self._worker_awaitable = asyncio.get_running_loop().create_task(
                self._worker())

    async def do(self, element):
        if not self._client_session:
            self._lazy_init()

        if self._worker_awaitable.done():
            await self._worker_awaitable
            raise Exception("JoinWithTable worker has already terminated")

        if element is _termination_obj:
            await self._q.put(_termination_obj)
            await self._worker_awaitable
        else:
            key = self._key_extractor(element)
            request = self._client_session.put(
                f'{self._webapi_url}/{self._table_path}/{key}',
                headers=self._get_item_headers,
                data=self._body, verify_ssl=False)
            task = asyncio.get_running_loop().create_task(request)
            await self._q.put((element, task))
            if self._worker_awaitable.done():
                await self._worker_awaitable


def build_flow(steps):
    if len(steps) == 0:
        print('Cannot build an empty flow')
    cur_step = steps[0]
    for next_step in steps[1:]:
        if isinstance(next_step, list):
            cur_step.to(build_flow(next_step))
        else:
            cur_step.to(next_step)
            cur_step = next_step
    return steps[0]
