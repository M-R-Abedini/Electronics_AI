"""An explicit document target must survive every public gateway layer."""
import asyncio
import pytest
from mcp import types
from suite_stdio import Suite, BackendSpec, result

@pytest.mark.parametrize('nested_target', [None, r'D:\other\second.PcbDoc'])
def test_nested_eda_invoker_preserves_target_and_rejects_conflicts(nested_target):
    async def run():
        suite=Suite([BackendSpec('eda', 'unused.exe')]); seen=[]
        async def request(method, **params):
            if method == 'list_tools':
                return types.ListToolsResult(tools=[
                    types.Tool(name='tool_invoke', inputSchema={'type':'object','properties':{
                        'name':{'type':'string'},'arguments':{'type':'object'},
                        'target_board_path':{'type':'string'}}}),
                    types.Tool(name='pcb_get_components', inputSchema={'type':'object','properties':{
                        'target_board_path':{'type':'string'}}})])
            seen.append(params['arguments']); return result({'success':True})
        suite.workers['eda'].request=request
        target=r'D:\test\board.PcbDoc'
        args={} if nested_target is None else {'target_board_path':nested_target}
        try:
            if nested_target:
                with pytest.raises(ValueError, match='conflicting explicit targets'):
                    await suite.invoke('eda__tool_invoke',{'name':'pcb_get_components','arguments':args},target_board_path=target)
                assert not seen
            else:
                await suite.invoke('eda__tool_invoke',{'name':'pcb_get_components','arguments':args},target_board_path=target)
                assert seen==[{'target_board_path':target}]
        finally: await suite.close()
    asyncio.run(run())


def test_native_target_refusal_keeps_code_in_public_tool_error_and_resets_scope():
    from eda_agent.bridge.targeting import targeted_tool, TARGET, ERROR
    from eda_agent.bridge.exceptions import AltiumCommandError
    async def tool():
        raise AltiumCommandError('loaded target required',code=ERROR)
    async def run():
        with pytest.raises(ValueError,match=ERROR):
            await targeted_tool(tool)(target_board_path=r'D:\test\board.PcbDoc')
        assert TARGET.get() is None
    asyncio.run(run())
