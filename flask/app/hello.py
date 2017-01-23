from flask import Flask, jsonify, render_template
from pymongo import MongoClient
import dns.resolver
import ipaddress
import time
from itertools import islice
from collections import Counter
import threading
from apscheduler.schedulers.background import BackgroundScheduler

_DEFAULT_ASN = 3701
_CUSTOMER_BGP_COMMUNITY = '3701:370'

app = Flask(__name__)


def db_connect():
    """Return a connection to the Mongo Database."""
    client = MongoClient(host='mongo')
    return(client.bgp)


def take(n, iterable):
    """Return first n items of the iterable as a list."""
    return list(islice(iterable, n))


def find_network(ip, netmask):
    """Given an IPv4 or IPv6 address, recursively search for and return the most
       specific prefix in the MongoDB collection.
    """
    try:
        if ipaddress.ip_address(ip).version == 4:
            db = db_connect()
            network = str(ipaddress.ip_network(
                ipaddress.ip_address(ip)).supernet(new_prefix=netmask))
            result = db.bgp.find_one({'prefix': network})
            if result is not None:
                return(result)
            elif netmask == 0:
                return(None)
            else:
                return(find_network(ip, netmask-1))
        elif ipaddress.ip_address(ip).version == 6:
            db = db_connect()
            network = str(ipaddress.ip_network(
                ipaddress.ip_address(ip)).supernet(new_prefix=netmask + 32))
            result = db.bgp.find_one({'prefix': network})
            if result is not None:
                return(result)
            elif netmask == 0:
                return(None)
            else:
                return(find_network(ip, netmask-1))
        else:
            return(None)
    except:
        return(None)


def asn_name_query(asn):
    """Given an *asn*, return the name."""
    if asn is None:
        asn = _DEFAULT_ASN
    if 64512 <= asn <= 65534:
        return('RFC6996 - Private Use ASN')
    else:
        try:
            query = 'as{number}.asn.cymru.com'.format(number=str(asn))
            resolver = dns.resolver.Resolver()
            answers = resolver.query(query, 'TXT')
            for rdata in answers:
                return(str(rdata).split('|')[-1].split(',', 2)[0].strip())
        except:
            return('(DNS Error)')


def is_peer(asn):
    """Is *asn* in the list of directy connected ASNs."""
    db = db_connect()
    if asn in db.bgp.distinct('next_hop_asn'):
        return True
    else:
        return False


def reverse_dns_query(ip):
    """Given an *ip*, return the reverse dns."""
    try:
        addr = dns.reversename.from_address(str(ip))
        resolver = dns.resolver.Resolver()
        return str(resolver.query(addr, 'PTR')[0])[:-1]
    except:
        return('(DNS Error)')


def peer_count():
    """Return the number of directly connected ASNs."""
    db = db_connect()
    return(len(db.bgp.distinct('next_hop_asn')))


def prefix_count(version):
    """Given the IP version, return the number of prefixes in the database."""
    db = db_connect()
    return(db.bgp.find({'ip_version': version}).count())


def nexthop_ip_count():
    """Return the number of unique next hop IPv4 and IPv6 addresses."""
    db = db_connect()
    return(len(db.bgp.distinct('nexthop')))


def epoch_to_date(epoch):
    """Given an *epoch* time stamp, return a human readable equivalent."""
    return(time.strftime('%Y-%m-%d %H:%M:%S %Z', time.gmtime(epoch)))


def avg_as_path_length():
    """Return the computed average *as_path* length of all prefixes in the
    database.  Using a python *set* to remove any AS prepending."""
    db = db_connect()
    as_path_counter = 0
    all_prefixes = db.bgp.find()
    for prefix in all_prefixes:
        try:
            as_path_counter += len(set(prefix['as_path']))
        except:
            pass
    return(round(as_path_counter/(all_prefixes.count() * 1.0), 3))


def top_peers(count):
    """Return a sorted list of top peer dictionaries ordered by prefix count.
    Limit to *count*."""
    db = db_connect()
    peers = {peer: db.bgp.find({'next_hop_asn': peer}).count()
             for peer in db.bgp.distinct('next_hop_asn')}
    return([{'asn': asn[0],
             'count': asn[1],
             'name': asn_name_query(asn[0])
             }
            for asn in take(count, sorted(peers.items(),
                                          key=lambda x: x[1],
                                          reverse=True))])


def peers():
    """Return a list of peer dictionaries."""
    db = db_connect()
    json_data = []
    myset = set()

    # customers_list = db.bgp.find({'communities': '3701:370'})
    peers_list = db.bgp.find()
    for prefix in peers_list:
        myset.add(prefix['next_hop_asn'])
    for asn in myset:
        ipv4_count = db.bgp.find({'next_hop_asn': asn, 'ip_version': 4}).count()
        ipv6_count = db.bgp.find({'next_hop_asn': asn, 'ip_version': 6}).count()
        if asn is None:
            asn = _DEFAULT_ASN
        json_data.append({
            'asn': asn,
            'name': asn_name_query(asn),
            'ipv4_count': ipv4_count,
            'ipv6_count': ipv6_count})
    return(json_data)


def customers():
    db = db_connect()
    json_data = []
    myset = set()
    ipv4_prefix_counter = 0
    ipv6_prefix_counter = 0

    customers_list = db.bgp.find({'communities': '3701:370'})
    for prefix in customers_list:
        myset.add(prefix['next_hop_asn'])
    for asn in myset:
        ipv4_count = db.bgp.find({'next_hop_asn': asn, 'ip_version': 4}).count()
        ipv6_count = db.bgp.find({'next_hop_asn': asn, 'ip_version': 6}).count()
        ipv4_prefix_counter += ipv4_count
        ipv6_prefix_counter += ipv6_count
        if asn is None:
            asn = _DEFAULT_ASN
        json_data.append({
            'asn': asn,
            'name': asn_name_query(asn),
            'ipv4_count': ipv4_count,
            'ipv6_count': ipv6_count})
    return(json_data, ipv4_prefix_counter, ipv6_prefix_counter)


def cidr_breakdown():
    db = db_connect()
    all_prefixes = db.bgp.find()
    ipv4_list = []
    ipv6_list = []
    json_data = []
    bads_list = []

    for prefix in all_prefixes:
        if prefix['ip_version'] == 4:
            ipv4_list.append(int(prefix['prefix'].split('/', 1)[1]))
            if int(prefix['prefix'].split('/', 1)[1]) > 24:
                bads_list.append({'origin_as': int(prefix['origin_as']),
                                  'prefix': prefix['prefix']})
        if prefix['ip_version'] == 6:
            ipv6_list.append(int(prefix['prefix'].split('/', 1)[1]))

    ipv4_count = list(Counter(ipv4_list).items())
    ipv6_count = list(Counter(ipv6_list).items())

    for mask, count in ipv4_count:
        json_data.append({
            'mask': mask,
            'count': count,
            'ip_version': 4})
    for mask, count in ipv6_count:
        json_data.append({
            'mask': mask,
            'count': count,
            'ip_version': 6})

    return(json_data)


def communities_count():
    db = db_connect()
    communities = db.bgp.distinct('communities')
    json_data = []

    for comm in communities:
        json_data.append({
            'community': comm,
            'count': db.bgp.find({'communities': {'$regex': comm}}).count()})

    return(json_data)


@app.route('/', methods=['GET'])
def index():
    # num_peers = peer_count()
    return render_template('home.html', **locals())


@app.route('/hello/', methods=['GET'])
def hello_index():
    data = myStats.get_data()
    top_peers = data['top_n_peers']
    cidr_breakdown = data['cidr_breakdown']
    communities = data['communities']
    peers = data['peers']
    return render_template('hello.html', **locals())


@app.route('/search/<query>', methods=['GET'])
def search_index(query):
    db = db_connect()
    number = 0
    prefixes = []
    for t in query.split():
        try:
            number = int(t)
        except:
            pass
    try:
        query = query.lower()
    except:
        pass
    network = find_network(query, netmask=32)
    if network is None:
        result = db.bgp.find({'$or': [{'next_hop_asn': int(number)},
                                      {'prefix': {'$regex': str(query)}}]})
        for network in result:
            prefixes.append({'origin_as': network['origin_as'],
                             'nexthop': network['nexthop'],
                             'as_path': network['as_path'],
                             'prefix': network['prefix'],
                             'next_hop_asn': network['next_hop_asn'],
                             'updated': epoch_to_date(network['timestamp']),
                             'name': asn_name_query(network['origin_as']),
                             'med': network['med'],
                             'local_pref': network['local_pref'],
                             'communities': network['communities']})
        return jsonify({'prefixes': prefixes})
    else:
        return jsonify({'origin_as': network['origin_as'],
                        'nexthop': network['nexthop'],
                        'as_path': network['as_path'],
                        'prefix': network['prefix'],
                        'next_hop_asn': network['next_hop_asn'],
                        'updated': epoch_to_date(network['timestamp']),
                        'name': asn_name_query(network['origin_as']),
                        'med': network['med'],
                        'local_pref': network['local_pref'],
                        'communities': network['communities']})


@app.route('/bgp/api/v1.0/ip/<ip>', methods=['GET'])
def get_ip(ip):
    network = find_network(ip, netmask=32)
    if network is None:
        return jsonify({})
    else:
        return jsonify({'origin_as': network['origin_as'],
                        'nexthop': network['nexthop'],
                        'as_path': network['as_path'],
                        'prefix': network['prefix'],
                        'next_hop_asn': network['next_hop_asn'],
                        'updated': epoch_to_date(network['timestamp']),
                        'name': asn_name_query(network['origin_as']),
                        'med': network['med'],
                        'local_pref': network['local_pref'],
                        'communities': network['communities']})


@app.route('/bgp/api/v1.0/asn/<int:asn>', methods=['GET'])
def get_asn_prefixes(asn):
    db = db_connect()
    prefixes = []

    if asn == _DEFAULT_ASN:
        routes = db.bgp.find({'origin_as': None})
    else:
        routes = db.bgp.find({'origin_as': asn})

    for prefix in routes:
        prefixes.append({'prefix': prefix['prefix'],
                         'origin_as': prefix['origin_as'],
                         'nexthop_ip': prefix['nexthop'],
                         'nexthop_ip_dns': reverse_dns_query(prefix['nexthop']),
                         'nexthop_asn': prefix['next_hop_asn'],
                         'as_path': prefix['as_path'],
                         'updated': epoch_to_date(prefix['timestamp']),
                         'name': asn_name_query(asn)})

    return jsonify({'asn': asn,
                    'name': asn_name_query(asn),
                    'origin_prefix_count': routes.count(),
                    'is_peer': is_peer(asn),
                    'origin_prefix_list': prefixes})


@app.route('/bgp/api/v1.0/peers', methods=['GET'])
def get_peers():
    db = db_connect()
    peers = []

    peer_asns = db.bgp.distinct('next_hop_asn')

    for asn in peer_asns:
        next_hop_ips = db.bgp.find({'next_hop_asn': asn}).distinct('nexthop')
        if asn is None:
            asn = _DEFAULT_ASN
        isp_origin_as = db.bgp.find({'origin_as': asn})
        isp_nexthop_as = db.bgp.find({'next_hop_asn': asn})
        if isp_nexthop_as.count() > isp_origin_as.count():
            transit_provider = True
        else:
            transit_provider = False
        peers.append({'asn': asn,
                      'name': asn_name_query(asn),
                      'next_hop_ips': next_hop_ips,
                      'origin_prefix_count': isp_origin_as.count(),
                      'nexthop_prefix_count': isp_nexthop_as.count(),
                      'transit_provider': transit_provider})

    return jsonify({'peers': peers})


@app.route('/bgp/api/v1.0/stats', methods=['GET'])
def get_stats():
    return myStats.get_json()


@app.route('/bgp/api/v1.0/asn/<int:asn>/transit', methods=['GET'])
def get_transit_prefixes(asn):
    db = db_connect()
    all_asns = db.bgp.find({})
    prefixes = []

    for prefix in all_asns:
        if prefix['as_path']:
            if asn in prefix['as_path']:
                prefixes.append(prefix['prefix'])
            else:
                pass
        else:
            pass

    return jsonify({'asn': asn,
                    'name': asn_name_query(asn),
                    'transit_prefix_count': len(prefixes),
                    'transit_prefix_list': prefixes})


class Stats(object):
    def __init__(self):
        self.peer_counter = 0
        self.ipv4_table_size = 0
        self.ipv6_table_size = 0
        self.nexthop_ip_counter = 0
        self.avg_as_path_length = 0
        self.top_n_peers = None
        self.cidr_breakdown = None
        self.communities = None
        self.peers = None
        self.customers = None
        self.customer_count = 0
        self.customer_ipv4_prefixes = 0
        self.customer_ipv6_prefixes = 0
        self.timestamp = epoch_to_date(time.time())

    def get_json(self):
        return jsonify({'peer_count': self.peer_counter,
                        'ipv4_table_size': self.ipv4_table_size,
                        'ipv6_table_size': self.ipv6_table_size,
                        'nexthop_ip_count': self.nexthop_ip_counter,
                        'avg_as_path_length': self.avg_as_path_length,
                        'top_n_peers': self.top_n_peers,
                        'cidr_breakdown': self.cidr_breakdown,
                        'communities': self.communities,
                        'peers': self.peers,
                        'customers': self.customers,
                        'customer_count': self.customer_count,
                        'customer_ipv4_prefixes': self.customer_ipv4_prefixes,
                        'customer_ipv6_prefixes': self.customer_ipv6_prefixes,
                        'timestamp': self.timestamp})

    def get_data(self):
        return ({'peer_count': self.peer_counter,
                 'ipv6_table_size': self.ipv6_table_size,
                 'ipv4_table_size': self.ipv4_table_size,
                 'nexthop_ip_count': self.nexthop_ip_counter,
                 'avg_as_path_length': self.avg_as_path_length,
                 'top_n_peers': self.top_n_peers,
                 'cidr_breakdown': self.cidr_breakdown,
                 'communities': self.communities,
                 'peers': self.peers,
                 'customers': self.customers,
                 'customer_count': self.customer_count,
                 'customer_ipv4_prefixes': self.customer_ipv4_prefixes,
                 'customer_ipv6_prefixes': self.customer_ipv6_prefixes,
                 'timestamp': self.timestamp})

    def update_stats(self):
        self.peer_counter = peer_count()
        self.ipv4_table_size = prefix_count(4)
        self.ipv6_table_size = prefix_count(6)
        self.nexthop_ip_counter = nexthop_ip_count()
        self.timestamp = epoch_to_date(time.time())
        self.customer_count = len(customers()[0])
        self.customer_ipv4_prefixes = customers()[1]
        self.customer_ipv6_prefixes = customers()[2]

    def update_advanced_stats(self):
        self.avg_as_path_length = avg_as_path_length()
        self.top_n_peers = top_peers(5)
        self.cidr_breakdown = cidr_breakdown()
        self.peers = peers()
        self.customers = customers()[0]
        self.communities = communities_count()
        self.timestamp = epoch_to_date(time.time())


sched = BackgroundScheduler()
myStats = Stats()
threading.Thread(target=myStats.update_stats).start()
threading.Thread(target=myStats.update_advanced_stats).start()
sched.add_job(myStats.update_stats, 'interval', seconds=5)
sched.add_job(myStats.update_advanced_stats, 'interval', seconds=90)
sched.start()

if __name__ == '__main__':
    app.run(debug=True)
