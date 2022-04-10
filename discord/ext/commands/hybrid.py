"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Type,
    TypeVar,
    Union,
    Optional,
)

import discord
import inspect
from discord import app_commands
from discord.utils import MISSING, maybe_coroutine, async_all
from .core import Command, Group
from .errors import CommandRegistrationError, CommandError, HybridCommandError, ConversionError
from .converter import Converter
from .cog import Cog

if TYPE_CHECKING:
    from typing_extensions import Self, ParamSpec, Concatenate
    from ._types import ContextT, Coro, BotT
    from .bot import Bot
    from .context import Context
    from .parameters import Parameter
    from discord.app_commands.commands import Check as AppCommandCheck


__all__ = (
    'HybridCommand',
    'HybridGroup',
    'hybrid_command',
    'hybrid_group',
)

T = TypeVar('T')
CogT = TypeVar('CogT', bound='Cog')
CommandT = TypeVar('CommandT', bound='Command')
# CHT = TypeVar('CHT', bound='Check')
GroupT = TypeVar('GroupT', bound='Group')

if TYPE_CHECKING:
    P = ParamSpec('P')
    P2 = ParamSpec('P2')

    CommandCallback = Union[
        Callable[Concatenate[CogT, ContextT, P], Coro[T]],
        Callable[Concatenate[ContextT, P], Coro[T]],
    ]
else:
    P = TypeVar('P')
    P2 = TypeVar('P2')


def is_converter(converter: Any) -> bool:
    return (inspect.isclass(converter) and issubclass(converter, Converter)) or isinstance(converter, Converter)


def make_converter_transformer(converter: Any) -> Type[app_commands.Transformer]:
    async def transform(cls, interaction: discord.Interaction, value: str) -> Any:
        try:
            if inspect.isclass(converter) and issubclass(converter, Converter):
                if inspect.ismethod(converter.convert):
                    return await converter.convert(interaction._baton, value)
                else:
                    return await converter().convert(interaction._baton, value)  # type: ignore
            elif isinstance(converter, Converter):
                return await converter.convert(interaction._baton, value)  # type: ignore
        except CommandError:
            raise
        except Exception as exc:
            raise ConversionError(converter, exc) from exc  # type: ignore

    return type('ConverterTransformer', (app_commands.Transformer,), {'transform': classmethod(transform)})


def replace_parameters(parameters: Dict[str, Parameter], signature: inspect.Signature) -> List[inspect.Parameter]:
    # Need to convert commands.Parameter back to inspect.Parameter so this will be a bit ugly
    params = signature.parameters.copy()
    for name, parameter in parameters.items():
        if is_converter(parameter.converter) and not hasattr(parameter.converter, '__discord_app_commands_transformer__'):
            params[name] = params[name].replace(annotation=make_converter_transformer(parameter.converter))

    return list(params.values())


class HybridAppCommand(discord.app_commands.Command[CogT, P, T]):
    def __init__(self, wrapped: HybridCommand[CogT, Any, T]) -> None:
        signature = inspect.signature(wrapped.callback)
        params = replace_parameters(wrapped.params, signature)
        wrapped.callback.__signature__ = signature.replace(parameters=params)

        try:
            super().__init__(
                name=wrapped.name,
                callback=wrapped.callback,  # type: ignore # Signature doesn't match but we're overriding the invoke
                description=wrapped.description or wrapped.short_doc or '…',
            )
        finally:
            del wrapped.callback.__signature__

        self.wrapped: HybridCommand[CogT, Any, T] = wrapped
        self.binding = wrapped.cog

    def _copy_with(self, **kwargs) -> Self:
        copy: Self = super()._copy_with(**kwargs)  # type: ignore
        copy.wrapped = self.wrapped
        return copy

    async def _check_can_run(self, interaction: discord.Interaction) -> bool:
        # Hybrid checks must run like so:
        # - Bot global check once
        # - Bot global check
        # - Parent interaction check
        # - Cog/group interaction check
        # - Cog check
        # - Local interaction checks
        # - Local command checks

        bot: Bot = interaction.client  # type: ignore
        ctx: Context[Bot] = interaction._baton

        if not await bot.can_run(ctx, call_once=True):
            return False

        if not await bot.can_run(ctx):
            return False

        if self.parent is not None and self.parent is not self.binding:
            # For commands with a parent which isn't the binding, i.e.
            # <binding>
            #     <parent>
            #         <command>
            # The parent check needs to be called first
            if not await maybe_coroutine(self.parent.interaction_check, interaction):
                return False

        if self.binding is not None:
            try:
                # Type checker does not like runtime attribute retrieval
                check: AppCommandCheck = self.binding.interaction_check  # type: ignore
            except AttributeError:
                pass
            else:
                ret = await maybe_coroutine(check, interaction)
                if not ret:
                    return False

            local_check = Cog._get_overridden_method(self.binding.cog_check)
            if local_check is not None:
                ret = await maybe_coroutine(local_check, ctx)
                if not ret:
                    return False

        if self.checks and not await async_all(f(interaction) for f in self.checks):  # type: ignore
            return False

        if self.wrapped.checks and not await async_all(f(ctx) for f in self.wrapped.checks):  # type: ignore
            return False

        return True

    async def _invoke_with_namespace(self, interaction: discord.Interaction, namespace: app_commands.Namespace) -> Any:
        # Wrap the interaction into a Context
        bot: Bot = interaction.client  # type: ignore

        # Unfortunately, `get_context` has to be called for this to work.
        # If someone doesn't inherit this to replace it with their custom class
        # then this doesn't work.
        interaction._baton = ctx = await bot.get_context(interaction)

        exc: CommandError
        try:
            await self.wrapped.prepare(ctx)
            # This lies and just always passes a Context instead of an Interaction.
            return await self._do_call(ctx, ctx.kwargs)  # type: ignore
        except app_commands.CommandSignatureMismatch:
            raise
        except app_commands.TransformerError as e:
            if isinstance(e.__cause__, CommandError):
                exc = e.__cause__
            else:
                exc = HybridCommandError(e)
                exc.__cause__ = e
        except app_commands.AppCommandError as e:
            exc = HybridCommandError(e)
            exc.__cause__ = e
        except CommandError as e:
            exc = e

        await self.wrapped.dispatch_error(ctx, exc)


class HybridCommand(Command[CogT, P, T]):
    r"""A class that is both an application command and a regular text command.

    This has the same parameters and attributes as a regular :class:`~discord.ext.commands.Command`.
    However, it also doubles as an :class:`application command <discord.app_commands.Command>`. In order
    for this to work, the callbacks must have the same subset that is supported by application
    commands.

    These are not created manually, instead they are created via the
    decorator or functional interface.

    .. versionadded:: 2.0
    """

    __commands_is_hybrid__: ClassVar[bool] = True

    def __init__(
        self,
        func: CommandCallback[CogT, ContextT, P, T],
        /,
        **kwargs,
    ) -> None:
        super().__init__(func, **kwargs)
        self.app_command: HybridAppCommand[CogT, Any, T] = HybridAppCommand(self)

    @property
    def cog(self) -> CogT:
        return self._cog

    @cog.setter
    def cog(self, value: CogT) -> None:
        self._cog = value
        self.app_command.binding = value

    async def can_run(self, ctx: Context[BotT], /) -> bool:
        if ctx.interaction is None:
            return await super().can_run(ctx)
        else:
            return await self.app_command._check_can_run(ctx.interaction)

    async def _parse_arguments(self, ctx: Context[BotT]) -> None:
        interaction = ctx.interaction
        if interaction is None:
            return await super()._parse_arguments(ctx)
        else:
            ctx.kwargs = await self.app_command._transform_arguments(interaction, interaction.namespace)


class HybridGroup(Group[CogT, P, T]):
    r"""A class that is both an application command group and a regular text group.

    This has the same parameters and attributes as a regular :class:`~discord.ext.commands.Group`.
    However, it also doubles as an :class:`application command group <discord.app_commands.Group>`.
    Note that application commands groups cannot have callbacks associated with them, so the callback
    is only called if it's not invoked as an application command.

    These are not created manually, instead they are created via the
    decorator or functional interface.

    .. versionadded:: 2.0
    """

    __commands_is_hybrid__: ClassVar[bool] = True

    def __init__(self, *args: Any, **attrs: Any) -> None:
        super().__init__(*args, **attrs)
        parent = None
        if self.parent is not None:
            if isinstance(self.parent, HybridGroup):
                parent = self.parent.app_command
            else:
                raise TypeError(f'HybridGroup parent must be HybridGroup not {self.parent.__class__}')

        guild_ids = attrs.pop('guild_ids', None) or getattr(self.callback, '__discord_app_commands_default_guilds__', None)
        self.app_command: app_commands.Group = app_commands.Group(
            name=self.name,
            description=self.description or self.short_doc or '…',
            guild_ids=guild_ids,
        )

        # This prevents the group from re-adding the command at __init__
        self.app_command.parent = parent

    def add_command(self, command: Union[HybridGroup[CogT, ..., Any], HybridCommand[CogT, ..., Any]], /) -> None:
        """Adds a :class:`.HybridCommand` into the internal list of commands.

        This is usually not called, instead the :meth:`~.GroupMixin.command` or
        :meth:`~.GroupMixin.group` shortcut decorators are used instead.

        Parameters
        -----------
        command: :class:`HybridCommand`
            The command to add.

        Raises
        -------
        CommandRegistrationError
            If the command or its alias is already registered by different command.
        TypeError
            If the command passed is not a subclass of :class:`.HybridCommand`.
        """

        if not isinstance(command, (HybridCommand, HybridGroup)):
            raise TypeError('The command passed must be a subclass of HybridCommand or HybridGroup')

        if isinstance(command, HybridGroup) and self.parent is not None:
            raise ValueError(f'{command.qualified_name!r} is too nested, groups can only be nested at most one level')

        self.app_command.add_command(command.app_command)
        command.parent = self

        if command.name in self.all_commands:
            raise CommandRegistrationError(command.name)

        self.all_commands[command.name] = command
        for alias in command.aliases:
            if alias in self.all_commands:
                self.remove_command(command.name)
                raise CommandRegistrationError(alias, alias_conflict=True)
            self.all_commands[alias] = command

    def remove_command(self, name: str, /) -> Optional[Command[CogT, ..., Any]]:
        cmd = super().remove_command(name)
        self.app_command.remove_command(name)
        return cmd

    def command(
        self,
        name: str = MISSING,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[CommandCallback[CogT, ContextT, P2, T]], HybridCommand[CogT, P2, T]]:
        """A shortcut decorator that invokes :func:`~discord.ext.commands.hybrid_command` and adds it to
        the internal command list via :meth:`add_command`.

        Returns
        --------
        Callable[..., :class:`Command`]
            A decorator that converts the provided method into a Command, adds it to the bot, then returns it.
        """

        def decorator(func: CommandCallback[CogT, ContextT, P2, T]):
            kwargs.setdefault('parent', self)
            result = hybrid_command(name=name, *args, **kwargs)(func)
            self.add_command(result)
            return result

        return decorator

    def group(
        self,
        name: str = MISSING,
        *args: Any,
        **kwargs: Any,
    ) -> Callable[[CommandCallback[CogT, ContextT, P2, T]], HybridGroup[CogT, P2, T]]:
        """A shortcut decorator that invokes :func:`.group` and adds it to
        the internal command list via :meth:`~.GroupMixin.add_command`.

        Returns
        --------
        Callable[..., :class:`Group`]
            A decorator that converts the provided method into a Group, adds it to the bot, then returns it.
        """

        def decorator(func: CommandCallback[CogT, ContextT, P2, T]):
            kwargs.setdefault('parent', self)
            result = hybrid_group(name=name, *args, **kwargs)(func)
            self.add_command(result)
            return result

        return decorator


def hybrid_command(
    name: str = MISSING,
    **attrs: Any,
) -> Callable[[CommandCallback[CogT, ContextT, P, T]], HybridCommand[CogT, P, T]]:
    """A decorator that transforms a function into a :class:`.HybridCommand`.

    A hybrid command is one that functions both as a regular :class:`.Command`
    and one that is also a :class:`app_commands.Command <discord.app_commands.Command>`.

    The callback being attached to the command must be representable as an
    application command callback. Converters are silently converted into a
    :class:`~discord.app_commands.Transformer` with a
    :attr:`discord.AppCommandOptionType.string` type.

    Checks and error handlers are dispatched and called as-if they were commands
    similar to :class:`.Command`. This means that they take :class:`Context` as
    a parameter rather than :class:`discord.Interaction`.

    All checks added using the :func:`.check` & co. decorators are added into
    the function. There is no way to supply your own checks through this
    decorator.

    .. versionadded:: 2.0

    Parameters
    -----------
    name: :class:`str`
        The name to create the command with. By default this uses the
        function name unchanged.
    attrs
        Keyword arguments to pass into the construction of the
        hybrid command.

    Raises
    -------
    TypeError
        If the function is not a coroutine or is already a command.
    """

    def decorator(func: CommandCallback[CogT, ContextT, P, T]):
        if isinstance(func, Command):
            raise TypeError('Callback is already a command.')
        return HybridCommand(func, name=name, **attrs)

    return decorator


def hybrid_group(
    name: str = MISSING,
    **attrs: Any,
) -> Callable[[CommandCallback[CogT, ContextT, P, T]], HybridGroup[CogT, P, T]]:
    """A decorator that transforms a function into a :class:`.HybridGroup`.

    This is similar to the :func:`~discord.ext.commands.group` decorator except it creates
    a hybrid group instead.
    """

    def decorator(func: CommandCallback[CogT, ContextT, P, T]):
        if isinstance(func, Command):
            raise TypeError('Callback is already a command.')
        return HybridGroup(func, name=name, **attrs)

    return decorator  # type: ignore
