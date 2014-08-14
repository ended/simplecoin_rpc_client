import yaml
import time
import os
import logging
import sys
import argparse
import datetime
import requests
import decimal

from tabulate import tabulate
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import sqlalchemy as sa

from urlparse import urljoin
from cryptokit.base58 import get_bcaddress_version
from itsdangerous import TimedSerializer, BadData

from bitcoinrpc.authproxy import JSONRPCException, CoinRPCException, AuthServiceProxy


base = declarative_base()


class Payout(base):
    """ Our single table in the sqlite database. Handles tracking the status of
    payouts and keeps track of tasks that needs to be retried, etc. """
    __tablename__ = "payouts"
    id = sa.Column(sa.Integer, primary_key=True)
    pid = sa.Column(sa.String, unique=True, nullable=False)
    user = sa.Column(sa.String, nullable=False)
    amount = sa.Column(sa.BigInteger(), nullable=False)
    txid = sa.Column(sa.String)
    associated = sa.Column(sa.Boolean, default=False, nullable=False)
    locked = sa.Column(sa.Boolean, default=False, nullable=False)

    # Times
    lock_time = sa.Column(sa.DateTime)
    paid_time = sa.Column(sa.DateTime)
    assoc_time = sa.Column(sa.DateTime)
    pull_time = sa.Column(sa.DateTime)

    @property
    def trans_id(self):
        if self.txid is None:
            return "NULL"
        return self.txid

    @property
    def amount_float(self):
        return self.amount / 100000000

    def tabulize(self, columns):
        return [getattr(self, a) for a in columns]


class RPCException(Exception):
    pass


class RPCClient(object):
    def _set_config(self, **kwargs):
        # A fast way to set defaults for the kwargs then set them as attributes
        base = os.path.abspath(os.path.dirname(__file__) + '/../')
        self.config = dict(coinserv=None,
                           valid_address_versions=[],
                           max_age=10,
                           logger_name="rpc",
                           log_level="INFO",
                           database_path=base + '/rpc.sqlite',
                           log_path=base + '/rpc.log')
        self.config.update(kwargs)

        required_conf = ['coinserv', 'valid_address_versions', 'currency_code',
                         'rpc_signature', 'rpc_url']
        error = False
        for req in required_conf:
            if req not in self.config:
                print("{} is a required configuration variable".format(req))
                error = True

        if error:
            exit(1)

    def __init__(self, config):
        if not config:
            print("Invalid configuration file")
            exit(1)
        self._set_config(**config)

        # setup our coinserver connection
        self.coinserv = AuthServiceProxy(
            "http://{0}:{1}@{2}:{3}/"
            .format(self.config['coinserv']['username'],
                    self.config['coinserv']['password'],
                    self.config['coinserv']['address'],
                    self.config['coinserv']['port'],
                    pool_kwargs=dict(maxsize=self.config.get('maxsize', 10))))

        # Setup the sqlite database mapper
        engine = sa.create_engine('sqlite:///{}'.format(self.config['database_path']), echo=self.config['log_level'] == "DEBUG")

        # Pulled from SQLA docs to implement strict exclusive access to the
        # payout state database.
        # See http://docs.sqlalchemy.org/en/rel_0_9/dialects/sqlite.html#pysqlite-serializable
        @sa.event.listens_for(engine, "connect")
        def do_connect(dbapi_connection, connection_record):
            # disable pysqlite's emitting of the BEGIN statement entirely.
            # also stops it from emitting COMMIT before any DDL.
            dbapi_connection.isolation_level = None

        @sa.event.listens_for(engine, "begin")
        def do_begin(conn):
            # emit our own BEGIN
            conn.execute("BEGIN EXCLUSIVE")

        self.db = sessionmaker(bind=engine)
        self.db.session = self.db()
        # Create the table if it doesn't exist
        Payout.__table__.create(engine, checkfirst=True)

        # Setup logger for the class
        logging.Formatter.converter = time.gmtime
        self.logger = logging.getLogger(self.config['logger_name'])
        self.logger.setLevel(getattr(logging, self.config['log_level']))
        log_format = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

        # stdout handler
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(log_format)
        handler.setLevel(getattr(logging, self.config['log_level']))
        self.logger.addHandler(handler)

        # don't attach a file handler if path evals false
        if self.config['log_path']:
            handler = logging.FileHandler(self.config['log_path'])
            handler.setFormatter(log_format)
            handler.setLevel(getattr(logging, self.config['log_level']))
            self.logger.addHandler(handler)

        self.serializer = TimedSerializer(self.config['rpc_signature'])

    def post(self, url, *args, **kwargs):
        if 'data' not in kwargs:
            kwargs['data'] = ''
        kwargs['data'] = self.serializer.dumps(kwargs['data'])
        return self.remote('/rpc/' + url, 'post', *args, **kwargs)

    def get(self, url, *args, **kwargs):
        return self.remote(url, 'get', *args, **kwargs)

    def remote(self, url, method, max_age=None, signed=True, **kwargs):
        url = urljoin(self.config['rpc_url'], url)
        self.logger.debug("Making request to {}".format(url))
        ret = getattr(requests, method)(url, timeout=270, **kwargs)
        if ret.status_code != 200:
            raise RPCException("Non 200 from remote: {}".format(ret.text))

        try:
            self.logger.debug("Got {} from remote".format(ret.text.encode('utf8')))
            if signed:
                return self.serializer.loads(ret.text, max_age or self.config['max_age'])
            else:
                return ret.json()
        except BadData:
            self.logger.error("Invalid data returned from remote!", exc_info=True)
            raise RPCException("Invalid signature")

    def poke_rpc(self, conn):
        try:
            conn.getinfo()
        except JSONRPCException:
            raise RPCException("Coinserver not awake")

    def confirm_trans(self, simulate=False):
        """ Grabs the unconfirmed transactions objects from the remote server
        and checks if they're confirmed. Also grabs and pushes the fees for the
        transaction if remote server supports it. """
        self.poke_rpc(self.coinserv)

        res = self.get('api/transaction?__filter_by={{"confirmed":false,"merged_type":{}}}'
                       .format(self.config['currency_code']), signed=False)

        if not res['success']:
            self.logger.error("Failure grabbing unconfirmed transactions: {}".format(res))
            return

        tids = []
        fees = {}
        for obj in res['objects']:
            self.logger.debug("Connecting to coinserv to lookup confirms for {}"
                              .format(obj['txid']))
            try:
                trans_data = self.coinserv.gettransaction(obj['txid'])
            except CoinRPCException:
                self.logger.error("Unable to fetch txid {} from rpc server!"
                                  .format(obj['txid']))
            except Exception:
                self.logger.error("Unable to fetch txid {} from rpc server!"
                                  .format(obj['txid']), exc_info=True)
            else:
                if trans_data['confirmations'] > self.config['trans_confirmations']:
                    tids.append(obj['txid'])
                    self.logger.info("Confirmed txid {} with {} confirms"
                                     .format(obj['txid'], trans_data['confirmations']))

                # grab and populate fee value if:
                # 1. Key is present in json from remote api (reverse compat)
                # 2. Key is not populated
                # 3. We got back a valid fee value from the rpc server
                if 'fee' in trans_data and not obj.get("fee") and 'fee' in obj:
                    assert isinstance(trans_data['fee'], decimal.Decimal)
                    fees[obj['txid']] = int(trans_data['fee'] * 100000000)
                    self.logger.info("Pushing fee value {} for txid {}"
                                     .format(trans_data['fee'], obj['txid']))

        if tids or fees:
            data = {'tids': tids, 'fees': fees}
            res = self.post('confirm_transactions', data=data)
            if res['result']:
                self.logger.info("Sucessfully confirmed transactions")  # XXX: Add number print outs
                return True

            self.logger.error("Failed to push confirmation information")
            return False
        else:
            self.logger.info("No valid transactions in need of fee value or confirmation")

    def reset_all_locked(self, simulate=False):
        """ Resets all locked payouts """
        payouts = self.db.session.query(Payout).filter_by(locked=True)
        self.logger.info("Resetting {:,} payout ids".format(payouts.count()))
        if simulate:
            self.logger.info("Just kidding, we're simulating... Exit.")
            exit(0)

        payouts.update({Payout.locked: False})
        self.db.session.commit()

    def pull_payouts(self, simulate=False):
        """ Gets all the unpaid payouts from the server """
        payouts = self.post(
            'get_payouts',
            data={'currency': self.config['currency_code']}
        )['pids']

        repeat = 0
        new = 0
        invalid = 0
        if not simulate:
            for user, amount, pid in payouts:
                if get_bcaddress_version(user) in self.config['valid_address_versions']:
                    if not self.db.session.query(Payout).filter_by(pid=pid).first():
                        p = Payout(pid=pid, user=user, amount=amount, pull_time=datetime.datetime.utcnow())
                        self.db.session.add(p)
                        new += 1
                    else:
                        repeat += 1
                else:
                    self.logger.warn("Ignoring payout {} due to invalid address"
                                     .format((user, amount, pid)))
                    invalid += 1

            self.db.session.commit()

        self.logger.info("Inserted {:,} new payouts and skipped {:,} old "
                         "payouts from the server. {:,} payouts with invalid addresses."
                         .format(new, repeat, invalid))
        return True

    def payout(self, simulate=False):
        """ Collects all the unpaid payout ids and pays them out """
        self.poke_rpc(self.coinserv)

        # Grab all now so that we use the same list of payouts for both
        # database transactions (locking, and unlocking)
        payouts = self.db.session.query(Payout).filter_by(txid=None, locked=False).all()
        if not payouts:
            self.logger.info("No payouts to process, exiting")
            return True

        # track the total payouts to each user
        user_payout_amounts = {}
        pids = {}
        for payout in payouts:
            user_payout_amounts.setdefault(payout.user, 0)
            user_payout_amounts[payout.user] += payout.amount
            pids.setdefault(payout.user, [])
            pids[payout.user].append(payout.pid)

            # We'll lock the payout before continuing in case of a failure in
            # between paying out and recording that payout action
            payout.locked = True
            payout.lock_time = datetime.datetime.utcnow()

        total_out = sum(user_payout_amounts.values())
        # Convert into satoshi
        balance = int(self.coinserv.getbalance() * 100000000)
        self.logger.info("Payout wallet balance: {:,}".format(balance / 100000000.0))
        self.logger.info("Total to be paid {:,}".format(total_out / 100000000.0))

        if balance < total_out:
            self.logger.error("Payout wallet is out of funds!")
            self.db.session.rollback()
            # XXX: Add an email call here
            return False

        if not simulate:
            self.db.session.commit()
        else:
            self.db.session.rollback()

        def format_pids(pids):
            pids = [str(a) for a in xrange(100)]
            lst = ", ".join(pids[:9])
            if len(pids) > 9:
                return lst + "... ({} more)".format(len(pids) - 8)
            return lst
        summary = [(user, amount / 100000000.0, format_pids(upids)) for
                   (user, amount), upids in zip(user_payout_amounts.iteritems(), pids.itervalues())]

        self.logger.info(
            "User payment summary\n" + tabulate(summary, headers=["User", "Total", "Pids"], tablefmt="grid"))

        try:
            if simulate:
                coin_txid = "1111111111111111111111111111111111111111111111111111111111111111"
                res = raw_input("Would you like the simulation to associate a "
                                "fake txid {} with these payouts? Don't do "
                                "this on production. [y/n] ".format(coin_txid))
                if res != "y":
                    self.logger.info("Exiting")
                    return True
            else:
                # now actually pay them
                user_payout_amounts["test"] = 1.1
                coin_txid = self.payout_many(user_payout_amounts)
        except CoinRPCException:
            new_balance = int(self.coinserv.getbalance() * 100000000)
            if new_balance != balance:
                self.logger.error(
                    "RPC error occured and wallet balance changed! Keeping the "
                    "payout entries locked. simplecoin_rpc dump_incomplete can "
                    "show you the details of the locked entries. If you're SURE"
                    "a double payout hasn't occured, use simplecoin_rpc "
                    "reset_all_locked to reset the entries.", exc_info=True)
                return False
            else:
                self.logger.error("RPC error occured and wallet balance didn't "
                                  "change. Unlocking payouts.")
                # Reset all the payouts so we can try again later
                for payout in payouts:
                    payout.locked = False
        else:
            # Success! Now associate the txid and unlock to allow association
            # with remote to occur
            for payout in payouts:
                payout.locked = False
                payout.txid = coin_txid
                payout.paid_time = datetime.datetime.utcnow()

            self.db.session.commit()
            self.logger.info("Updated {:,} payouts with txid {}"
                             .format(len(payouts), coin_txid))
            return coin_txid

    def _tabulate(self, title, query, headers=None):
        """ Displays a table of payouts given a query to fetch payouts with, a
        title to label the table, and an optional list of columns to display
        """
        print("@@ {} @@".format(title))
        headers = headers if headers else ["pid", "user", "amount_float", "associated", "locked", "trans_id"]
        data = [p.tabulize(headers) for p in query]
        if data:
            print(tabulate(data, headers=headers, tablefmt="grid"))
        else:
            print("-- Nothing to display --")
        print("")

    def dump_incomplete(self, simulate=False, unpaid_locked=True, paid_unassoc=True, unpaid_unlocked=True):
        """ Prints out a nice display of all incomplete payout records. """
        if unpaid_locked:
            self.unpaid_locked()
        if paid_unassoc:
            self.paid_unassoc()
        if unpaid_unlocked:
            self.unpaid_unlocked()

    def unpaid_locked(self):
        self._tabulate(
            "Unpaid locked payouts",
            self.db.session.query(Payout).filter_by(txid=None, locked=True))

    def paid_unassoc(self):
        self._tabulate(
            "Paid un-associated payouts",
            self.db.session.query(Payout).filter_by(associated=False).filter(Payout.txid != None))

    def unpaid_unlocked(self):
        self._tabulate(
            "Payouts ready to payout",
            self.db.session.query(Payout).filter_by(txid=None, locked=False))

    def associate_all(self, simulate=False):
        txids = {}
        payouts = self.db.session.query(Payout).filter_by(associated=False).filter(Payout.txid != None)
        for payout in payouts:
            txids.setdefault(payout.txid, [])
            txids[payout.txid].append(payout)

        for txid, payouts in txids.iteritems():
            if simulate:
                self.logger.info("Would attempt remote association of {:,} ids "
                                 "with txid {}".format(len(payouts), txid))
            else:
                self.associate(txid, payouts)

    def associate(self, txid, payouts):
        pids = [p.pid for p in payouts]
        self.logger.info("Trying to associate {:,} payouts with txid {} on remote"
                         .format(len(payouts), txid))

        data = {'coin_txid': txid, 'pids': pids, 'currency': self.config['currency_code']}
        self.logger.info("Associating {:,} payout ids and with txid {}"
                         .format(len(pids), txid))

        res = self.post('update_payouts', data=data)
        if res['result']:
            self.logger.info("Recieved success response from the server.")
            for payout in payouts:
                payout.associated = True
                payout.assoc_time = datetime.datetime.utcnow()
                self.db.session.commit()
            return True
        return False

    def payout_many(self, recip):
        self.coinserv = self.coinserv
        fee = self.config['payout_fee']
        passphrase = self.config['coinserv']['wallet_pass']
        account = self.config['coinserv']['account']

        if passphrase:
            wallet = self.coinserv.walletpassphrase(passphrase, 10)
            self.logger.info("Unlocking wallet: %s" % wallet)
        self.logger.info("Setting tx fee: %s" % self.coinserv.settxfee(fee))
        self.logger.info("Sending to recip: " + str(recip))
        self.logger.info("Sending from account: " + str(account))
        return self.coinserv.sendmany(account, recip)

    def call(self, command, **kwargs):
        try:
            return getattr(self, command)(**kwargs)
        except Exception:
            self.logger.error("Unhandled exception calling {} with {}"
                              .format(command, kwargs), exc_info=True)
            return False


def entry():
    parser = argparse.ArgumentParser(prog='simplecoin RPC')
    parser.add_argument('-c', '--config', default='config.yml', type=argparse.FileType('r'))
    parser.add_argument('-l', '--log-level',
                        choices=['DEBUG', 'INFO', 'WARN', 'ERROR'])
    parser.add_argument('-s', '--simulate', action='store_true', default=False)
    subparsers = parser.add_subparsers(title='main subcommands', dest='action')

    subparsers.add_parser('confirm_trans',
                          help='fetches unconfirmed transactions and tries to confirm them')
    subparsers.add_parser('payout', help='pays out all ready payout records')
    subparsers.add_parser('pull_payouts', help='pulls down new payouts that are ready from the server')
    subparsers.add_parser('reset_all_locked', help='resets all locked payouts')
    subparsers.add_parser('dump_incomplete', help='')
    subparsers.add_parser('associate_all', help='')

    args = parser.parse_args()

    global_args = ['log_level', 'action', 'config']
    # subcommand functions shouldn't recieve arguments directed at the
    # global object/ configs
    kwargs = {k: v for k, v in vars(args).iteritems() if k not in global_args}

    config = yaml.load(args.config)
    if args.log_level:
        config['log_level'] = args.log_level
    interface = RPCClient(config)
    interface.call(args.action, **kwargs)
