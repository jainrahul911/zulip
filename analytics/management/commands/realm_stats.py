import datetime
from argparse import ArgumentParser
from typing import Any, List

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils.timezone import now as timezone_now

from zerver.models import (
    Message,
    Realm,
    Recipient,
    Stream,
    Subscription,
    UserActivity,
    UserMessage,
    UserProfile,
    get_realm,
)

MOBILE_CLIENT_LIST = ["Android", "ios"]
HUMAN_CLIENT_LIST = MOBILE_CLIENT_LIST + ["website"]

human_messages = Message.objects.filter(sending_client__name__in=HUMAN_CLIENT_LIST)

class Command(BaseCommand):
    help = "Generate statistics on realm activity."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument('realms', metavar='<realm>', type=str, nargs='*',
                            help="realm to generate statistics for")

    def active_users(self, realm: Realm) -> List[UserProfile]:
        # Has been active (on the website, for now) in the last 7 days.
        activity_cutoff = timezone_now() - datetime.timedelta(days=7)
        return [activity.user_profile for activity in (
            UserActivity.objects.filter(user_profile__realm=realm,
                                        user_profile__is_active=True,
                                        last_visit__gt=activity_cutoff,
                                        query="/json/users/me/pointer",
                                        client__name="website"))]

    def messages_sent_by(self, user: UserProfile, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return human_messages.filter(sender=user, date_sent__gt=sent_time_cutoff).count()

    def total_messages(self, realm: Realm, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return Message.objects.filter(sender__realm=realm, date_sent__gt=sent_time_cutoff).count()

    def human_messages(self, realm: Realm, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return human_messages.filter(sender__realm=realm, date_sent__gt=sent_time_cutoff).count()

    def api_messages(self, realm: Realm, days_ago: int) -> int:
        return (self.total_messages(realm, days_ago) - self.human_messages(realm, days_ago))

    def stream_messages(self, realm: Realm, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return human_messages.filter(sender__realm=realm, date_sent__gt=sent_time_cutoff,
                                     recipient__type=Recipient.STREAM).count()

    def private_messages(self, realm: Realm, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return human_messages.filter(sender__realm=realm, date_sent__gt=sent_time_cutoff).exclude(
            recipient__type=Recipient.STREAM).exclude(recipient__type=Recipient.HUDDLE).count()

    def group_private_messages(self, realm: Realm, days_ago: int) -> int:
        sent_time_cutoff = timezone_now() - datetime.timedelta(days=days_ago)
        return human_messages.filter(sender__realm=realm, date_sent__gt=sent_time_cutoff).exclude(
            recipient__type=Recipient.STREAM).exclude(recipient__type=Recipient.PERSONAL).count()

    def report_percentage(self, numerator: float, denominator: float, text: str) -> None:
        if not denominator:
            fraction = 0.0
        else:
            fraction = numerator / float(denominator)
        print(f"{fraction * 100:.2f}% of", text)

    def handle(self, *args: Any, **options: Any) -> None:
        if options['realms']:
            try:
                realms = [get_realm(string_id) for string_id in options['realms']]
            except Realm.DoesNotExist as e:
                raise CommandError(e)
        else:
            realms = Realm.objects.all()

        for realm in realms:
            print(realm.string_id)

            user_profiles = UserProfile.objects.filter(realm=realm, is_active=True)
            active_users = self.active_users(realm)
            num_active = len(active_users)

            print(f"{num_active} active users ({len(user_profiles)} total)")
            streams = Stream.objects.filter(realm=realm).extra(
                tables=['zerver_subscription', 'zerver_recipient'],
                where=['zerver_subscription.recipient_id = zerver_recipient.id',
                       'zerver_recipient.type = 2',
                       'zerver_recipient.type_id = zerver_stream.id',
                       'zerver_subscription.active = true']).annotate(count=Count("name"))
            print(f"{streams.count()} streams")

            for days_ago in (1, 7, 30):
                print(f"In last {days_ago} days, users sent:")
                sender_quantities = [self.messages_sent_by(user, days_ago) for user in user_profiles]
                for quantity in sorted(sender_quantities, reverse=True):
                    print(quantity, end=' ')
                print("")

                print(f"{self.stream_messages(realm, days_ago)} stream messages")
                print(f"{self.private_messages(realm, days_ago)} one-on-one private messages")
                print(f"{self.api_messages(realm, days_ago)} messages sent via the API")
                print(f"{self.group_private_messages(realm, days_ago)} group private messages")

            num_notifications_enabled = len([x for x in active_users if x.enable_desktop_notifications])
            self.report_percentage(num_notifications_enabled, num_active,
                                   "active users have desktop notifications enabled")

            num_enter_sends = len([x for x in active_users if x.enter_sends])
            self.report_percentage(num_enter_sends, num_active,
                                   "active users have enter-sends")

            all_message_count = human_messages.filter(sender__realm=realm).count()
            multi_paragraph_message_count = human_messages.filter(
                sender__realm=realm, content__contains="\n\n").count()
            self.report_percentage(multi_paragraph_message_count, all_message_count,
                                   "all messages are multi-paragraph")

            # Starred messages
            starrers = UserMessage.objects.filter(user_profile__in=user_profiles,
                                                  flags=UserMessage.flags.starred).values(
                "user_profile").annotate(count=Count("user_profile"))
            print("{} users have starred {} messages".format(
                len(starrers), sum([elt["count"] for elt in starrers])))

            active_user_subs = Subscription.objects.filter(
                user_profile__in=user_profiles, active=True)

            # Streams not in home view
            non_home_view = active_user_subs.filter(is_muted=True).values(
                "user_profile").annotate(count=Count("user_profile"))
            print("{} users have {} streams not in home view".format(
                len(non_home_view), sum([elt["count"] for elt in non_home_view])))

            # Code block markup
            markup_messages = human_messages.filter(
                sender__realm=realm, content__contains="~~~").values(
                "sender").annotate(count=Count("sender"))
            print("{} users have used code block markup on {} messages".format(
                len(markup_messages), sum([elt["count"] for elt in markup_messages])))

            # Notifications for stream messages
            notifications = active_user_subs.filter(desktop_notifications=True).values(
                "user_profile").annotate(count=Count("user_profile"))
            print("{} users receive desktop notifications for {} streams".format(
                len(notifications), sum([elt["count"] for elt in notifications])))

            print("")
