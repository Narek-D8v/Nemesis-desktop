from nemus.core.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = 'echo_tool'
    description = 'Повторяет введенное сообщение.'
    parameters = {'type':'object','properties':{'message':{'type':'string','description':'Сообщение для повтора'}},'required':['message']}
    def execute(self, message='', **_):
        return ToolResult.success(f'Эхо: {message}')
