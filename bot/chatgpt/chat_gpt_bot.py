# encoding:utf-8

import time

import openai
# import openai.error
import requests
import json

from bot.bot import Bot
from bot.chatgpt.chat_gpt_session import ChatGPTSession
from bot.openai.open_ai_image import OpenAIImage
from bot.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.token_bucket import TokenBucket
from config import conf, load_config


def split_text_into_chunks(text, num_chunks):
    # 确保输入的段数合理
    if num_chunks <= 0:
        raise ValueError("num_chunks must be a positive integer")

    # 根据双换行符号分割文本
    paragraphs = text.split('\n\n')

    # 计算每段的平均长度
    avg_length = len(paragraphs) // num_chunks

    chunks = []
    current_chunk = []
    current_length = 0

    for paragraph in paragraphs:
        current_chunk.append(paragraph)
        current_length += 1

        # 如果当前段落数达到平均长度，并且还没有到最后一个段，则结束当前段
        if current_length >= avg_length and len(chunks) < num_chunks - 1:
            chunks.append('\n\n'.join(current_chunk))
            current_chunk = []
            current_length = 0

    # 将剩余的段落加入最后一段
    if current_chunk:
        chunks.append('\n\n'.join(current_chunk))

    return chunks


# OpenAI对话模型API (可用)
class ChatGPTBot(Bot, OpenAIImage):
    def __init__(self):
        super().__init__()
        # set the default api_key
        openai.api_key = conf().get("open_ai_api_key")
        if conf().get("open_ai_api_base"):
            openai.api_base = conf().get("open_ai_api_base")
        proxy = conf().get("proxy")
        if proxy:
            openai.proxy = proxy
        if conf().get("rate_limit_chatgpt"):
            self.tb4chatgpt = TokenBucket(conf().get("rate_limit_chatgpt", 20))

        self.sessions = SessionManager(ChatGPTSession, model=conf().get("model") or "gpt-3.5-turbo")
        self.args = {
            "model": conf().get("model") or "gpt-3.5-turbo",  # 对话模型的名称
            "temperature": conf().get("temperature", 0.9),  # 值在[0,1]之间，越大表示回复越具有不确定性
            # "max_tokens":4096,  # 回复最大的字符数
            "top_p": conf().get("top_p", 1),
            "frequency_penalty": conf().get("frequency_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "presence_penalty": conf().get("presence_penalty", 0.0),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "request_timeout": conf().get("request_timeout", None),  # 请求超时时间，openai接口默认设置为600，对于难问题一般需要较长时间
            "timeout": conf().get("request_timeout", None),  # 重试超时时间，在这个时间内，将会自动重试
        }

    def reply(self, query, context=None):
        # acquire reply content
        if context.type == ContextType.TEXT:
            logger.info("[CHATGPT] query={}".format(query))

            session_id = context["session_id"]
            reply = None
            clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
            if query in clear_memory_commands:
                self.sessions.clear_session(session_id)
                reply = Reply(ReplyType.INFO, "记忆已清除")
            elif query == "#清除所有":
                self.sessions.clear_all_session()
                reply = Reply(ReplyType.INFO, "所有人记忆已清除")
            elif query == "#更新配置":
                load_config()
                reply = Reply(ReplyType.INFO, "配置已更新")
            elif query == "我在测试消息跳转":
                load_config()
                reply = ["<a href=\"weixin://bizmsgmenu?msgmenucontent=我膝盖不舒服&msgmenuid=1\">请点击文字</a>"]
            elif query == "满意":
                load_config()
                reply = ["感谢您的评价"]
            elif query == "不满意":
                load_config()
                reply = ["感谢您的评价，请发送 “#反馈 ” 开头的消息给我们提供反馈。如果您对医学回答不满意，请发送您的问题，后台的医生看到消息后会给您提供更专业的医学回答。如果您对产品功能有不满意，请发送您的意见，我们会根据您的意见进行优化。谢谢！"]
            if reply:
                return reply
            session = self.sessions.session_query(query, session_id)
            logger.debug("[CHATGPT] session query={}".format(session.messages))

            api_key = context.get("openai_api_key")
            model = context.get("gpt_model")
            new_args = None
            if model:
                new_args = self.args.copy()
                new_args["model"] = model
            # if context.get("stream"):
                # reply in stream
            return self.reply_text_stream(session, api_key, args=new_args)
            # reply_content = self.reply_text(session, api_key, args=new_args)
            # logger.debug(
            #     "[CHATGPT] new_query={}, session_id={}, reply_cont={}, completion_tokens={}".format(
            #         session.messages,
            #         session_id,
            #         reply_content["content"],
            #         reply_content["completion_tokens"],
            #     )
            # )
            # if reply_content["completion_tokens"] == 0 and len(reply_content["content"]) > 0:
            #     reply = Reply(ReplyType.ERROR, reply_content["content"])
            # elif reply_content["completion_tokens"] > 0:
            #     self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
            #     reply = Reply(ReplyType.TEXT, reply_content["content"])
            # else:
            #     reply = Reply(ReplyType.ERROR, reply_content["content"])
            #     logger.debug("[CHATGPT] reply {} used 0 tokens.".format(reply_content))
            # return reply

        elif context.type == ContextType.IMAGE_CREATE:
            ok, retstring = self.create_img(query, 0)
            reply = None
            if ok:
                reply = Reply(ReplyType.IMAGE_URL, retstring)
            else:
                reply = Reply(ReplyType.ERROR, retstring)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    def reply_text_stream(self, session: ChatGPTSession, api_key=None, args=None, retry_count=0):
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """
        try:
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise Exception("RateLimitError: rate limit exceeded")
            # if api_key == None, the default openai.api_key will be used
            if args is None:
                args = self.args
            # response = openai.ChatCompletion.create(api_key=api_key, messages=session.messages, **args)

            payload = {
                "message": str(session.messages[-1]["content"]),
                "history": [history_record for history_record in session.messages[1:-1]],
            }
            url = "http://localhost:8000/api/chat"

            headers = {
                'Content-Type': 'application/json'
            }

            logger.info("[CHATGPT] query={}".format(payload))

            response_str = ""
            response_prev = ""
            response = requests.post(url, json=payload, headers=headers, stream=True)

            if response.headers.get('content-type') == 'text/plain; charset=utf-8':
                logger.info("[CHATGPT] Answer from library: " + response.content.decode('utf-8'))
                # response_str = (response.content.decode('utf-8') + "\n问题回答完毕").split('\n\n')
                response_str = split_text_into_chunks(response.content.decode('utf-8') + "\n问题回答完毕", 5)
                for line in response_str:
                    yield line
            else:
                for line in response.iter_content(chunk_size=4096):
                    if line:
                        response_str = line.decode('utf-8').replace('*', '')
                        if len(response_str) - len(response_prev) > 300:
                            # find the last \n in response_str
                            last_newline = response_str.rfind('\n')
                            if last_newline != -1:
                                # logger.info("yield response_str: " + response_str[:last_newline])
                                # logger.info("response_prev: " + response_prev)
                                yield response_str[len(response_prev):last_newline].strip()
                                response_prev = response_str[:last_newline]
                            else:
                                # logger.info("yield response_str_no_line: " + response_str)
                                # logger.info("response_prev_no_line: " + response_prev)
                                yield response_str[len(response_prev):].strip()
                                response_prev = response_str

                logger.info("final response body: " + response_str)

                str_response = (response_str[len(response_prev):] + "\n问题回答完毕").strip()

                logger.info("last response_str: " + str_response)

                yield str_response
        except Exception as e:
            need_retry = retry_count < 2
            result = {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}
            # if isinstance(e, openai.error.RateLimitError):
            #     logger.warn("[CHATGPT] RateLimitError: {}".format(e))
            #     result["content"] = "提问太快啦，请休息一下再问我吧"
            #     if need_retry:
            #         time.sleep(20)
            # elif isinstance(e, openai.error.Timeout):
            #     logger.warn("[CHATGPT] Timeout: {}".format(e))
            #     result["content"] = "我没有收到你的消息"
            #     if need_retry:
            #         time.sleep(5)
            # elif isinstance(e, openai.error.APIError):
            #     logger.warn("[CHATGPT] Bad Gateway: {}".format(e))
            #     result["content"] = "请再问我一次"
            #     if need_retry:
            #         time.sleep(10)
            # elif isinstance(e, openai.error.APIConnectionError):
            #     logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
            #     result["content"] = "我连接不到你的网络"
            #     if need_retry:
            #         time.sleep(5)
            # else:
            logger.exception("[CHATGPT] Exception: {}".format(e))
            need_retry = False
            self.sessions.clear_session(session.session_id)

            if need_retry:
                logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
                return self.reply_text(session, api_key, args, retry_count + 1)
            else:
                return result

    def reply_text(self, session: ChatGPTSession, api_key=None, args=None, retry_count=0) -> dict:
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """
        try:
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise Exception("RateLimitError: rate limit exceeded")
            # if api_key == None, the default openai.api_key will be used
            if args is None:
                args = self.args
            # response = openai.ChatCompletion.create(api_key=api_key, messages=session.messages, **args)

            payload = {
                "message": str(session.messages[-1]["content"]),
                "history": [history_record for history_record in session.messages[1:-1]],
            }
            url = "http://localhost:8000/api/chat"

            headers = {
                'Content-Type': 'application/json'
            }

            # response = requests.request("POST", url, headers=headers, data=payload)

            response_str = ""

            # s = requests.Session()
            # with s.get(url, headers=None, stream=True) as resp:
            #     for line in resp.iter_lines():
            #         if line:
            #             response_str += line.decode("utf-8")

            response = requests.post(url, json=payload, headers=headers, stream=True)

            for line in response.iter_content(chunk_size=2048):
                if line:
                    response_str = line.decode('utf-8')

            logger.info("response_str: " + response_str)

            try:
                json.loads(response_str)
                json_response = json.loads(response_str)['answer']
            except ValueError as e:
                json_response = response_str

            return {
                "total_tokens": 1,
                "completion_tokens": 1,
                "content": json_response
            }
        except Exception as e:
            need_retry = retry_count < 2
            result = {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}
            # if isinstance(e, openai.error.RateLimitError):
            #     logger.warn("[CHATGPT] RateLimitError: {}".format(e))
            #     result["content"] = "提问太快啦，请休息一下再问我吧"
            #     if need_retry:
            #         time.sleep(20)
            # elif isinstance(e, openai.error.Timeout):
            #     logger.warn("[CHATGPT] Timeout: {}".format(e))
            #     result["content"] = "我没有收到你的消息"
            #     if need_retry:
            #         time.sleep(5)
            # elif isinstance(e, openai.error.APIError):
            #     logger.warn("[CHATGPT] Bad Gateway: {}".format(e))
            #     result["content"] = "请再问我一次"
            #     if need_retry:
            #         time.sleep(10)
            # elif isinstance(e, openai.error.APIConnectionError):
            #     logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
            #     result["content"] = "我连接不到你的网络"
            #     if need_retry:
            #         time.sleep(5)
            # else:
            logger.exception("[CHATGPT] Exception: {}".format(e))
            need_retry = False
            self.sessions.clear_session(session.session_id)

            if need_retry:
                logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
                return self.reply_text(session, api_key, args, retry_count + 1)
            else:
                return result


class AzureChatGPTBot(ChatGPTBot):
    def __init__(self):
        super().__init__()
        openai.api_type = "azure"
        openai.api_version = conf().get("azure_api_version", "2023-06-01-preview")
        self.args["deployment_id"] = conf().get("azure_deployment_id")

    def create_img(self, query, retry_count=0, api_key=None):
        api_version = "2022-08-03-preview"
        url = "{}dalle/text-to-image?api-version={}".format(openai.api_base, api_version)
        api_key = api_key or openai.api_key
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        try:
            body = {"caption": query, "resolution": conf().get("image_create_size", "256x256")}
            submission = requests.post(url, headers=headers, json=body)
            operation_location = submission.headers["Operation-Location"]
            retry_after = submission.headers["Retry-after"]
            status = ""
            image_url = ""
            while status != "Succeeded":
                logger.info("waiting for image create..., " + status + ",retry after " + retry_after + " seconds")
                time.sleep(int(retry_after))
                response = requests.get(operation_location, headers=headers)
                status = response.json()["status"]
            image_url = response.json()["result"]["contentUrl"]
            return True, image_url
        except Exception as e:
            logger.error("create image error: {}".format(e))
            return False, "图片生成失败"
