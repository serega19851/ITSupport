from textwrap import dedent
from typing import Callable

from django.db.transaction import atomic
from django.utils import timezone
from telegram.ext import CallbackQueryHandler
from telegram.ext import CommandHandler
from telegram.ext import Filters
from telegram.ext import MessageHandler
from telegram.ext import Updater
from telegram.ext.callbackcontext import CallbackContext
from telegram.update import Update

from support_app.models import BotUser
from support_app.models import Manager
from support_app.models import Contractor
from support_app.models import Client
from support_app.models import Order
from support_app.models import SystemSettings

import logging

logger = logging.getLogger('tgbot_app_error')


def get_user(func: Callable) -> Callable:
    """Decorator to add user in context when telegram handlers starts"""

    def wrapper(update: Update, context: CallbackContext) -> str:
        chat_id = update.effective_chat.id
        username = update.effective_user.username

        try:
            active_users = BotUser.objects.active()
            try:
                user = active_users.get(telegram_id=chat_id)
                if user.tg_nick != username:
                    user.tg_nick = username
                    user.save()
            except BotUser.DoesNotExist:
                user = active_users.get(tg_nick=username)
                if user.telegram_id != chat_id:
                    user.telegram_id = chat_id
                    user.save()
        except BotUser.DoesNotExist:
            user = None

        context.user_data['user'] = user
        return func(update, context)

    return wrapper


class TgBot(object):

    def __init__(self, tg_token: str, states_functions: dict[str, dict[str, Callable]]) -> None:
        """
            states_functions not dict[str, Callable] because it contains many bots like:
            states_functions = {
                'bot_1': {
                    'state_1_bot_1': func1_bot1,
                    'state_2_bot_1': func2_bot1,
                },
                'bot_2': {
                    'state_1_bot_2': func1_bot2,
                    'state_2_bot_2': func2_bot2,
                }
            }
        """
        self.tg_token = tg_token
        self.states_functions = states_functions
        self.updater = Updater(token=tg_token, use_context=True)
        self.updater.dispatcher.add_handler(CommandHandler('start', get_user(self.handle_users_reply)))
        self.updater.dispatcher.add_handler(CommandHandler('help', self.help_handler))
        self.updater.dispatcher.add_handler(CallbackQueryHandler(get_user(self.handle_users_reply)))
        self.updater.dispatcher.add_handler(MessageHandler(Filters.text, get_user(self.handle_users_reply)))
        self.updater.dispatcher.add_error_handler(self.error)
        self.job_queue = self.updater.job_queue

        self.job_queue.run_repeating(
            self.handle_warning_orders_not_in_work,
            interval=60,
            first=10,
            name='handle_warning_orders_not_in_work'
        )

        self.job_queue.run_repeating(
            self.handle_warning_orders_not_closed,
            interval=60,
            first=20,
            name='handle_warning_orders_not_closed'
        )

        self.job_queue.run_repeating(
            self.handle_new_orders_inform,
            interval=60,
            first=30,
            name='handle_warning_orders_not_closed'
        )

    def handle_users_reply(self, update: Update, context: CallbackContext) -> None:
        """
        State machine of bot.

        Current state of user record to DB
        """
        user = context.user_data['user']

        if user is None:
            self.states_functions['unknown']['START'](update, context)
            return

        if update.message:
            user_reply = update.message.text
        elif update.callback_query:
            user_reply = update.callback_query.data
        else:
            return

        if user_reply == '/start':
            user_state = 'START'
        else:
            user_state = user.bot_state
            user_state = user_state if user_state else 'START'

        state_handler = self.states_functions[user.role][user_state]
        next_state = state_handler(update, context)
        user.bot_state = next_state
        user.save()

    def error(self, update: Update, context: CallbackContext) -> None:
        """Error handler"""
        print(f'Update "{update}" caused error "{context.error}"')
        logger.error(f'caused error "{context.error}"')
        raise context.error

    def help_handler(self, update: Update, context: CallbackContext) -> None:
        """help handler"""
        update.message.reply_text("Используйте /start для того, что бы перезапустить бот")

    def handle_warning_orders_not_in_work(self, context: CallbackContext) -> None:
        """If there are an overdue created orders they should be sent to every manager"""
        warning_orders_not_in_work = Order.objects.get_warning_orders_not_in_work()
        if not warning_orders_not_in_work:  # если не будет просроченных заказов, то не отправляем
            return
        message = 'Есть заказы, которые долго не берут в работу\n\n' + '\n'.join(
            [
                f'Задача: {order.task} \nКонтакт клиента: @{order.client.tg_nick}\n\n'
                for order in warning_orders_not_in_work
            ]
        )
        managers = Manager.objects.active()
        for manager in managers:
            context.bot.send_message(text=message, chat_id=manager.telegram_id)
        warning_orders_not_in_work.update(not_in_work_manager_informed=True)

    def handle_warning_orders_not_closed(self, context: CallbackContext) -> None:
        """If there are an overdue in work orders they should be sent to every manager"""
        warning_orders_not_closed = Order.objects.get_warning_orders_not_closed()
        if not warning_orders_not_closed:  # если не будет просроченных заказов, то не отправляем
            return
        message = 'Есть заказы, которые не выполнены\n\n' + '\n'.join(
            [
                f'Задача: {order.task}\nКонтакт подрядчика: @{order.contractor.tg_nick}\nКонтакт клиента: @{order.client.tg_nick}\n\n'
                for order in warning_orders_not_closed
            ]
        )
        managers = Manager.objects.active()
        for manager in managers:
            context.bot.send_message(text=message, chat_id=manager.telegram_id)
        warning_orders_not_closed.update(late_work_manager_informed=True)

    def handle_new_orders_inform(self, context: CallbackContext) -> None:
        """If there are a new orders contractors should be informed"""
        new_orders = Order.objects.get_available_not_informed_all().select_related(
            'client',
            'client__tariff',
        )
        available_contractors = Contractor.objects.get_available()

        try:
            assigned_contractors_limit = SystemSettings.objects.get(
                'ASSIGNED_CONTRACTORS_TIME_LIMIT'
            ).parameter_value
            assigned_contractors_limit = int(assigned_contractors_limit) / 100
        except (SystemSettings.DoesNotExist, ValueError):
            assigned_contractors_limit = 0.2

        # it can be not optimal if many new orders but it should be about 5 orders in hour
        # so it not a big chance to have more then 2 orders simultaneously
        # also it send message to many contractors but telegram don't have bulk message :(
        for new_order in new_orders:
            # only send message without button
            # because contractor can do anything and button can broke his process
            message = dedent(f'''
            Появился новый заказ, для взятие в работу нажмите "Посмотреть заказы"
            и выберите данный заказ:

            Задание:
            {new_order.task}
            ''')
            client = new_order.client
            if not client.contractors.all():
                # if no assigned contractors then send all available and mark then
                # send both - assigned and all
                for contractor in available_contractors:
                    contractor_chat_id = contractor.telegram_id
                    context.bot.send_message(text=message, chat_id=contractor_chat_id)
                with atomic():
                    new_order.assigned_contractors_informed = True
                    new_order.all_contractors_informed = True
                    new_order.save()
            else:
                self.process_new_order_with_contractors(
                    context,
                    new_order,
                    client,
                    assigned_contractors_limit,
                    message,
                )

    def process_new_order_with_contractors(
            self,
            context: CallbackContext,
            new_order: Order,
            client: Client,
            assigned_contractors_limit: float,
            message: str,
    ):
        # check if time for assigned contractors or all
        client_tariff_reaction_time_seconds = new_order.client.tariff.reaction_time_minutes * 60
        seconds_from_created = (timezone.now() - new_order.created_at).total_seconds()
        is_inform_only_assigned_contractors = False
        if seconds_from_created / client_tariff_reaction_time_seconds < assigned_contractors_limit:
            is_inform_only_assigned_contractors = True

        # check that assigned contractors weren't informed too
        if is_inform_only_assigned_contractors and not new_order.assigned_contractors_informed:
            for assigned_contractor in client.contractors.all():
                contractor_chat_id = assigned_contractor.contractor.telegram_id
                context.bot.send_message(text=message, chat_id=contractor_chat_id)
                new_order.assigned_contractors_informed = True
                new_order.save()
        elif not is_inform_only_assigned_contractors:
            # inform all contractors except assigned
            for contractor in client.get_not_assigned_contractors():
                contractor_chat_id = contractor.telegram_id
                context.bot.send_message(text=message, chat_id=contractor_chat_id)
                with atomic():
                    new_order.assigned_contractors_informed = True  # just for be sure
                    new_order.all_contractors_informed = True
                    new_order.save()
