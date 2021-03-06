from persistent.mapping import PersistentMapping
from persistent.list import PersistentList
from persistent import Persistent

from datetime import datetime
from uuid import uuid4
import itertools
import math
from fractions import Fraction

import trueskill


class Player(Persistent):
    """
    A Player plays matches. He is local to a Game, so a single physical Person could be two distinct 'Player' objects.
    This is because the Player has a skill rating associated with it, which only makes sense for
    his skill in a single game.
    """

    def __init__(self, name):
        self.name = name
        # Trueskill Rating for this player
        self._rating = trueskill.Rating()
        # List of matches this player participated in
        self.matches = PersistentList()
        # History of skill
        self.history = PersistentList()

    def add_match(self, match):
        self.matches.append(match)
        self.history.append({
            'match': match,
            'skill' : self.skill(),
            'confidence' : self.confidence(),
            'exposure' : self.exposure()
        })

    def set_rating(self, rating):
        self._rating = rating
        self._p_changed = True

    def get_rating(self):
        return self._rating

    def reset_rating(self):
        self.set_rating(trueskill.Rating())

    def skill(self):
        return self._rating.mu

    def confidence(self):
        return self._rating.sigma

    def exposure(self):
        return trueskill.expose(self._rating)

    def wins(self):
        return [m for m in self.matches if m.won(self)]

    def losses(self):
        return [m for m in self.matches if not m.won(self) and not m.draw()]
        
    def __repr__(self):
        return self.name


class Match(Persistent):
    """
    A Match is composed of two teams and a score/winner of the match
    """

    def __init__(self, game, teams, score):
        self.date = datetime.now()
        self.game = game

        self.teams = teams
        self.score = score

        self.uuid = uuid4()

        self.init_stats()

    def init_stats(self):
        a_win_probability = self.game.win_probability(self.teams[0], self.teams[1])
        frac = Fraction(int(max(round(a_win_probability * 10), 1)), int(max(round((1 - a_win_probability) * 10), 1)))
        self.stats = {
            'odds_a': frac.numerator,
            'odds_b': frac.denominator
        }

        player_ratigns = {}
        for p in self.players():
            player_ratigns[p.name] = p.exposure()
        self.stats['rating_changes'] = player_ratigns

    def update_rating_delta(self):
        player_ratings = self.stats['rating_changes']
        for p in self.players():
            player_ratings[p.name] = p.exposure() - player_ratings[p.name]

    def team_a_won(self):
        return self.score[0] > self.score[1]

    def draw(self):
        return self.score[0] == self.score[1]

    def team_b_won(self):
        return self.score[1] > self.score[0]

    def participated(self, player):
        return player in self.teams[0] or player in self.teams[1]

    def won(self, player):
        return (player in self.teams[0] and self.team_a_won()) \
               or (player in self.teams[1] and self.team_b_won())

    def players(self):
        return self.teams[0] + self.teams[1]
        
    def __str__(self):
        return "%s: Team A: %s, Team B: %s, Score: %d - %d" %(str(self.date), str(self.teams[0]), str(self.teams[1]), self.score[0], self.score[1])


class Game(Persistent):
    """
    A Game aggregates the players and matches that are part of a competition. For example, a Game could be 'Football'
    or 'Hockey'
    """

    def __init__(self, name):
        self.name = name
        # Player name -> Player
        self.players = PersistentMapping()
        # List of all matches for this game
        self.matches = PersistentList()
        # Whether to use average instead of sum-of-skill for this game
        self.use_average_team_skill = True

    def delete_match(self, match):
        if not match in self.matches:
            return

        self.matches.remove(match)

        players = match.teams[0] + match.teams[1]
        for p in players:
            if match in p.matches:
                p.matches.remove(match)

        self.recalculate_ratings()

        for p in list(self.players.keys()):
            if not self.players[p].matches:
                self.players.pop(p)

    def add_match(self, teams, score):
        players_a = [self.get_player(name) for name in teams[0]]
        players_b = [self.get_player(name) for name in teams[1]]

        # Add Match to the Database
        match = Match(self, [players_a, players_b], score)
        self.matches.append(match)

        self.update_player_ratings(match)
        match.update_rating_delta()

    def update_player_ratings(self, match):
        ratings_a = [p.get_rating() for p in match.teams[0]]
        ratings_b = [p.get_rating() for p in match.teams[1]]

        # Sort by score and get rank indices
        rank = list(zip(match.score, range(len(match.score))))
        rank.sort(key=lambda r: r[0], reverse=True)
        rank_indices = list(zip(*rank))[1]

        # Check for Draw
        # TODO: make this generic for more than 2 teams
        if match.score[0] == match.score[1]:
            rank_indices = [0, 0]

        # Calculate new Ratings using trueskill algorithm
        new_ratings = trueskill.rate([ratings_a, ratings_b], ranks=rank_indices,
                                     weights=self.team_weights(ratings_a, ratings_b))

        for r, p in zip(new_ratings[0], match.teams[0]):
            p.set_rating(r)
            p.add_match(match)

        for r, p in zip(new_ratings[1], match.teams[1]):
            p.set_rating(r)
            p.add_match(match)

    def recalculate_ratings(self):
        for player in self.players.values():
            player.reset_rating()
            player.matches.clear()
            player.history.clear()

        for match in self.matches:
            match.init_stats()
            self.update_player_ratings(match)
            match.update_rating_delta()

    def get_player(self, name):
        if not name in self.players:
            self.players[name] = Player(name)

        return self.players[name]

    # Calcualte player weights for a match based on whether average or additive ratings
    # are used for this game
    def team_weights(self, team1, team2):
        ratings = [team1, team2]
        weights = [[1 for _ in r] for r in ratings]
        if self.use_average_team_skill:
            # Adjust weights by team-size. This effectively causes the TrueSkill algorithm to
            # look at the average instead of the sum of skills
            min_team_size = min(map(len, ratings))
            weights = [[min_team_size / float(len(r)) for _ in r] for r in ratings]
        return weights

    def win_probability(self, team1, team2):
        """"
        Calculate the win probability of team1 over team2 given the skill ratings of
        all the players in the teams.
        """

        def skill_sum(team, weights):
            return sum([v.skill()*w for (v,w) in zip(team, weights)])

        weights = self.team_weights(team1, team2)
        delta_mu = skill_sum(team1, weights[0]) - skill_sum(team2, weights[1])
        sum_sigma = sum((r.confidence() * w) ** 2 for (r, w) in zip(itertools.chain(team1, team2), itertools.chain(*weights)))
        size = len(team1) + len(team2)
        denom = math.sqrt(size * (trueskill.BETA * trueskill.BETA) + sum_sigma)
        ts = trueskill.global_env()
        return ts.cdf(delta_mu / denom)

    def draw_probability(self, team1, team2):
        r1 = [p.get_rating() for p in team1]
        r2 = [p.get_rating() for p in team2]
        return trueskill.quality([r1, r2], weights=self.team_weights(team1, team2))


class Context(PersistentMapping):
    __parent__ = __name__ = None

    # Bump this anytime the model changes, and add migration code to upgrade_db() so old databases are updated on load
    db_version = 4

    def __init__(self):
        # The current version of this database instance. Used to determine if a database upgrade is necessary
        self.db_version = Context.db_version

        # Add default game pages
        self.games = PersistentMapping()
        self.games["volleyball"] = Game('Volleyball')
        self.games['kicker'] = Game('Kicker')


def upgrade_db(context):
    """" Upgrade the database to a newer version if neccessary. If any persistent objects' structure changes, the
         db_version of the Context class should be bumped, and migration code which appropriately modifies the 'old'
         database objects to fit the 'new' version should be added at the very bottom of the function like so:

         if context.db_version == <old version>:
            <Upgrade code>
            context.db_version = <new version>
    """

    if Context.db_version == context.db_version:
        return

    # Add Rating history to all players
    if (context.db_version == 1 or context.db_version == 2):
        for g in context.games.values():
            for p in g.players.values():
                p.history = PersistentList()
                p._p_changed = True
            g.recalculate_ratings()
        context.db_version = 3

    if (context.db_version == 3):
        for g in context.games.values():
            g.use_average_team_skill = True
            for m in g.matches:
                m.game = g
            g.recalculate_ratings()
        context.db_version = 4


def appmaker(zodb_root):
    if not 'app_root' in zodb_root:
        app_root = Context()
        zodb_root['app_root'] = app_root
        import transaction
        transaction.commit()

    ctx = zodb_root['app_root']
    upgrade_db(ctx)
    return ctx
